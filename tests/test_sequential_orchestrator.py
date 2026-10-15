from __future__ import annotations

import subprocess
import re
import json
from pathlib import Path

import pytest

from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.molicoder.evidence_view import render_task_evidence
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult, ProviderStatus
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.molicoder.executor import ExecutionResult
from local_model_chat.molicoder.repair import RepairOutcome
from local_model_chat.storage import ChatStore


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _project(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    (root / "first.txt").write_text("base\n", encoding="utf-8")
    store = ChatStore(tmp_path / "db.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root, store, project


class Planner:
    provider = "codex"

    def plan(self, _packet):
        return parse_plan(
            {
                "objective": "Sequential change",
                "tasks": [
                    {"key": "one", "title": "One", "objective": "Create first output", "risk": "low", "depends_on": [], "expected_paths": ["one.txt"]},
                    {"key": "two", "title": "Two", "objective": "Use first output", "risk": "low", "depends_on": ["one"], "expected_paths": ["two.txt"]},
                ],
            }
        )


class Executor:
    name = "codex"
    model = ""

    def __init__(self):
        self.paths = []

    def execute(self, worktree, prompt, *, timeout_seconds=1200):
        worktree = Path(worktree)
        self.paths.append(worktree)
        if "(one)" in prompt:
            (worktree / "one.txt").write_text("one\n", encoding="utf-8")
        else:
            assert (worktree / "one.txt").read_text(encoding="utf-8") == "one\n"
            (worktree / "two.txt").write_text("two\n", encoding="utf-8")
        return ProviderResult("completed", "done", seconds=0.1)

    def cancel(self):
        return None


@pytest.mark.parametrize("provider_status", ["needs_attention", "completed"])
def test_opaque_agent_stops_for_review_before_retry_or_handoff(tmp_path, provider_status) -> None:
    root, store, project = _project(tmp_path)
    events = []

    class OneTaskPlanner:
        provider = "fixture"

        def plan(self, _packet):
            return parse_plan({"objective": "One change", "tasks": [{
                "key": "one", "title": "One", "objective": "Create output",
                "risk": "low", "depends_on": [], "expected_paths": ["one.txt"],
            }]})

    class OpaqueExecutor(Executor):
        enforcement_class = "opaque_agent"

        def execute(self, worktree, _prompt, *, timeout_seconds=1200):
            self.paths.append(Path(worktree))
            (Path(worktree) / "one.txt").write_text("unreviewed\n", encoding="utf-8")
            return ProviderResult(provider_status, "attempted", error="quota exceeded")

    class Alternate(Executor):
        def execute(self, *_args, **_kwargs):
            raise AssertionError("opaque agent outcome must not trigger a handoff")

    opaque = OpaqueExecutor()
    result = SequentialRunOrchestrator(
        store=store, project=project, planner=OneTaskPlanner(), provider=opaque,
        alternate_provider=Alternate(), validate=lambda _root: "validation failed",
        worktree_base=tmp_path / "opaque-worktrees", max_task_attempts=3,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("Opaque execution", mode="FRONTIER_ONLY")

    task = store.list_run_tasks(result.run_id)[0]
    assert result.status == "needs_attention" and task.status == "review_required"
    assert task.attempts == 1 and len(opaque.paths) == 1
    assert result.integration_path and (result.integration_path / "one.txt").read_text() == "unreviewed\n"
    assert (root / "first.txt").read_text() == "base\n"
    assert any(kind == "opaque_attempt_review_required" for kind, _payload in events)
    assert not any(kind in {"model_handoff", "repair_started"} for kind, _payload in events)


def test_tasks_share_one_worktree_and_success_auto_applies(tmp_path) -> None:
    root, store, project = _project(tmp_path)
    executor = Executor()
    orchestrator = SequentialRunOrchestrator(
        store=store,
        project=project,
        planner=Planner(),
        provider=executor,
        validate=lambda _root: "validation passed",
        worktree_base=tmp_path / "worktrees",
        auto_apply=True,
        route_decision={"selected": "local:test", "reason": "verified local route",
                        "constraints": ["tools verified"], "alternatives": [],
                        "usage_accuracy": "local_zero"},
    )

    result = orchestrator.run("Make sequential changes", mode="HYBRID")

    assert result.status == "completed"
    assert len(set(executor.paths)) == 1
    assert (root / "one.txt").read_text(encoding="utf-8") == "one\n"
    assert (root / "two.txt").read_text(encoding="utf-8") == "two\n"
    assert _git(root, "status", "--porcelain") == ""
    route_events = [event for event in store.list_events(result.run_id) if event.kind == "routing_decision"]
    assert len(route_events) == 1
    assert json.loads(route_events[0].payload_json)["selected"] == "local:test"
    for task in store.list_run_tasks(result.run_id):
        evidence = store.get_task_completion(task.id)
        assert evidence is not None
        assert evidence.revision and evidence.validation_summary.startswith("validation passed")
        assert evidence.output_artifacts and evidence.unmet_criteria == ()
        assert evidence.human_review_status == "not_required"


def test_unverified_acceptance_criterion_keeps_checkpoint_for_review(tmp_path) -> None:
    root, store, project = _project(tmp_path)

    class CriterionPlanner:
        provider = "codex"

        def plan(self, _packet):
            return parse_plan({
                "objective": "Add output", "tasks": [{
                    "key": "one", "title": "One", "objective": "Create first output",
                    "risk": "low", "depends_on": [], "expected_paths": ["one.txt"],
                    "acceptance_criteria": ["Output has been reviewed by a person"],
                    "verification_checks": ["project-default"],
                }],
            })

    result = SequentialRunOrchestrator(
        store=store, project=project, planner=CriterionPlanner(), provider=Executor(),
        validate=lambda _root: "validation passed", worktree_base=tmp_path / "criteria-worktrees",
        auto_apply=True,
    ).run("Create output", mode="FRONTIER_ONLY")

    task = store.list_run_tasks(result.run_id)[0]
    evidence = store.get_task_completion(task.id)
    assert result.status == "needs_attention" and task.status == "review_required"
    assert evidence is not None and evidence.unmet_criteria == ("Output has been reviewed by a person",)
    assert evidence.human_review_status == "pending"
    assert result.integration_path and (result.integration_path / "one.txt").is_file()
    assert not (root / "one.txt").exists()


def test_failed_attempt_is_preserved_before_retry_from_accepted_checkpoint(tmp_path) -> None:
    root, store, project = _project(tmp_path)
    events = []

    class OneTaskPlanner:
        provider = "codex"

        def plan(self, _packet):
            return parse_plan({"objective": "One change", "tasks": [{
                "key": "one", "title": "One", "objective": "Create first output",
                "risk": "low", "depends_on": [], "expected_paths": ["one.txt"],
            }]})

    class RetryExecutor(Executor):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def execute(self, worktree, prompt, *, timeout_seconds=1200):
            self.calls += 1
            worktree = Path(worktree)
            if self.calls == 1:
                (worktree / "bad.txt").write_text("failed output", encoding="utf-8")
            else:
                assert not (worktree / "bad.txt").exists()
                (worktree / "one.txt").write_text("accepted output", encoding="utf-8")
            return ProviderResult("completed", "done", seconds=0.01)

    executor = RetryExecutor()
    result = SequentialRunOrchestrator(
        store=store, project=project, planner=OneTaskPlanner(), provider=executor,
        validate=lambda _root: "validation passed", max_task_attempts=2,
        worktree_base=tmp_path / "retry-worktrees", auto_apply=True,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("Retry from checkpoint", mode="FRONTIER_ONLY")

    assert result.status == "completed" and executor.calls == 2
    assert (root / "one.txt").read_text() == "accepted output"
    assert not (root / "bad.txt").exists()
    saved = [payload for kind, payload in events if kind == "failed_attempt_preserved"]
    assert len(saved) == 1 and len(saved[0]["stash_commit"]) == 40
    assert saved[0]["accepted_checkpoint"] == result.base_commit


def test_frontier_planning_call_enters_same_run_usage_trail(tmp_path) -> None:
    _root, store, project = _project(tmp_path)
    orchestrator = SequentialRunOrchestrator(
        store=store, project=project, planner=Planner(), provider=Executor(),
        validate=lambda _root: "validation passed",
    )
    run_id = store.create_run(project.id, "Plan", "FRONTIER_ONLY")
    orchestrator._active_run_id = run_id
    orchestrator._provider_event("provider_finished", {
        "provider": "codex", "purpose": "planning", "status": "completed",
        "input_tokens": 10, "output_tokens": 4, "seconds": 0.2,
    })
    calls = store.list_external_calls(run_id)
    assert len(calls) == 1
    assert calls[0]["purpose"] == "planning"
    assert calls[0]["input_tokens"] == 10


def test_validation_failure_preserves_worktree_and_never_touches_checkout(tmp_path) -> None:
    root, store, project = _project(tmp_path)
    executor = Executor()
    orchestrator = SequentialRunOrchestrator(
        store=store,
        project=project,
        planner=Planner(),
        provider=executor,
        validate=lambda _root: "validation failed\npytest: exit 1",
        worktree_base=tmp_path / "failed-worktrees",
        auto_apply=True,
    )

    result = orchestrator.run("Fail safely", mode="FRONTIER_ONLY")

    assert result.status == "needs_attention"
    assert not (root / "one.txt").exists()
    assert result.integration_path and result.integration_path.is_dir()
    assert _git(root, "status", "--porcelain") == ""


def test_quota_failure_stops_same_provider_retry_and_preserves_worktree(tmp_path) -> None:
    _root, store, project = _project(tmp_path)
    events = []

    class QuotaExecutor(Executor):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def execute(self, worktree, prompt, *, timeout_seconds=1200):
            self.calls += 1
            return ProviderResult("needs_attention", "", error="quota exceeded")

    executor = QuotaExecutor()
    result = SequentialRunOrchestrator(
        store=store, project=project, planner=Planner(), provider=executor,
        validate=lambda _root: "validation passed",
        worktree_base=tmp_path / "quota-worktrees", max_task_attempts=3,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("Stop on quota", mode="FRONTIER_ONLY")

    assert result.status == "needs_attention"
    assert executor.calls == 1
    assert result.integration_path and result.integration_path.is_dir()
    assert any(kind == "provider_failure_action" and payload["action"] == "wait_for_reset"
               for kind, payload in events)
    assert any(kind == "task_recovery_decision" and payload["category"] == "provider"
               and payload["action"] == "wait_for_reset" for kind, payload in events)
    task = store.list_run_tasks(result.run_id)[0]
    assert task.status == "waiting_quota"
    assert store.list_task_state_history(task.id)[-1]["reason"] == "quota exceeded"


def test_quota_handoff_uses_alternate_with_verified_progress_and_one_remaining_attempt(tmp_path) -> None:
    root, store, project = _project(tmp_path)
    events = []

    class Primary(Executor):
        name = "primary"

        def __init__(self):
            super().__init__()
            self.calls = 0

        def execute(self, worktree, prompt, *, timeout_seconds=1200):
            self.calls += 1
            worktree = Path(worktree)
            if "(one)" in prompt:
                (worktree / "one.txt").write_text("one\n", encoding="utf-8")
                return ProviderResult("completed", "done")
            (worktree / "bad.txt").write_text("discard before handoff", encoding="utf-8")
            return ProviderResult("needs_attention", "", error="quota exceeded")

    class Alternate(Executor):
        name = "alternate"

        def __init__(self):
            super().__init__()
            self.calls = 0

        def probe(self):
            return ProviderStatus(self.name, True, True, auth_verified=True)

        def execute(self, worktree, prompt, *, timeout_seconds=1200):
            self.calls += 1
            worktree = Path(worktree)
            assert (worktree / "one.txt").read_text(encoding="utf-8") == "one\n"
            assert not (worktree / "bad.txt").exists()
            assert "Verified prior work:" in prompt and "One" in prompt
            assert "quota exceeded" in prompt and "remaining attempts 1" in prompt
            (worktree / "two.txt").write_text("two\n", encoding="utf-8")
            return ProviderResult("completed", "done")

    primary, alternate = Primary(), Alternate()
    result = SequentialRunOrchestrator(
        store=store, project=project, planner=Planner(), provider=primary,
        alternate_provider=alternate, validate=lambda _root: "validation passed",
        worktree_base=tmp_path / "handoff-worktrees", max_task_attempts=2,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ).run("Use an alternate after quota", mode="FRONTIER_ONLY")
    assert result.status == "completed"
    assert primary.calls == 2 and alternate.calls == 1
    assert (root / "one.txt").is_file() and (root / "two.txt").is_file()
    assert not (root / "bad.txt").exists()
    assert any(kind == "model_handoff" and payload["remaining_attempts"] == 1 for kind, payload in events)
    assert any(kind == "failed_attempt_preserved" for kind, _payload in events)
    evidence = render_task_evidence(store, result.run_id)
    assert "Model handoff: alternate — quota exceeded" in evidence
    assert "Failed attempt saved:" in evidence


def test_handoff_stops_when_alternate_repeats_same_failure(tmp_path) -> None:
    _root, store, project = _project(tmp_path)

    class FailingProvider:
        model = "fixture"

        def __init__(self, name):
            self.name = name
            self.calls = 0

        def probe(self):
            return ProviderStatus(self.name, True, True, auth_verified=True)

        def execute(self, _worktree, _prompt, *, timeout_seconds=1200):
            self.calls += 1
            return ProviderResult("needs_attention", "", error="quota exceeded")

        def cancel(self):
            pass

    primary, alternate = FailingProvider("primary"), FailingProvider("alternate")
    result = SequentialRunOrchestrator(
        store=store, project=project, planner=Planner(), provider=primary,
        alternate_provider=alternate, validate=lambda _root: "validation passed",
        worktree_base=tmp_path / "same-failure-worktrees", max_task_attempts=4,
    ).run("Stop a repeated failure", mode="FRONTIER_ONLY")
    assert result.status == "needs_attention"
    assert primary.calls == 1 and alternate.calls == 1
    assert "same provider failure loop stopped" in result.error


def test_handoff_does_not_exceed_single_attempt_budget(tmp_path) -> None:
    _root, store, project = _project(tmp_path)

    class QuotaProvider:
        name = "primary"
        model = "fixture"

        def execute(self, _worktree, _prompt, *, timeout_seconds=1200):
            return ProviderResult("needs_attention", "", error="quota exceeded")

        def cancel(self):
            pass

    class Alternate(QuotaProvider):
        name = "alternate"

        def __init__(self):
            self.calls = 0

        def probe(self):
            return ProviderStatus(self.name, True, True, auth_verified=True)

        def execute(self, _worktree, _prompt, *, timeout_seconds=1200):
            self.calls += 1
            raise AssertionError("attempt budget was exceeded")

    alternate = Alternate()
    result = SequentialRunOrchestrator(
        store=store, project=project, planner=Planner(), provider=QuotaProvider(),
        alternate_provider=alternate, validate=lambda _root: "validation passed",
        worktree_base=tmp_path / "budget-worktrees", max_task_attempts=1,
    ).run("Keep attempt cap", mode="FRONTIER_ONLY")
    assert result.status == "needs_attention" and alternate.calls == 0


def test_local_external_action_review_stops_before_provider_retry(tmp_path) -> None:
    root, store, project = _project(tmp_path)

    class LocalTaskPlanner:
        provider = "fixture"

        def plan(self, _packet):
            return parse_plan({"objective": "Update first", "tasks": [{
                "key": "one", "title": "First", "objective": "Update first.txt",
                "risk": "low", "depends_on": [], "expected_paths": ["first.txt"],
            }]})

    class UncalledProvider(Executor):
        def execute(self, *_args, **_kwargs):
            raise AssertionError("external action review must stop before provider retry")

    local_calls = []
    def local_executor(_workspace, _prompt):
        local_calls.append(1)
        execution = ExecutionResult(
            "review_required", "", 1, 0, "unsupported external action requires user review"
        )
        return RepairOutcome("review_required", 1, execution, (), execution.error, (execution,))

    result = SequentialRunOrchestrator(
        store=store, project=project, planner=LocalTaskPlanner(), provider=UncalledProvider(),
        local_executor=local_executor, validate=lambda _root: "validation passed",
        worktree_base=tmp_path / "external-action-worktrees", max_task_attempts=2,
    ).run("Update first", mode="LOCAL_OFFLINE")
    task = store.list_run_tasks(result.run_id)[0]
    assert result.status == "needs_attention" and task.status == "review_required"
    assert local_calls == [1]
    assert (root / "first.txt").read_text(encoding="utf-8") == "base\n"


def test_multi_task_resume_keeps_preserved_worktree_for_separate_evidence_review(tmp_path) -> None:
    root, store, project = _project(tmp_path)
    first_executor = Executor()
    first = SequentialRunOrchestrator(
        store=store,
        project=project,
        planner=Planner(),
        provider=first_executor,
        validate=lambda _root: "validation failed\nmissing remaining work",
        worktree_base=tmp_path / "resume-worktrees",
        auto_apply=True,
    )
    stopped = first.run("Resume this build", mode="FRONTIER_ONLY")
    assert stopped.status == "needs_attention"

    class ResumeExecutor(Executor):
        def execute(self, worktree, prompt, *, timeout_seconds=1200):
            worktree = Path(worktree)
            assert (worktree / "one.txt").is_file()
            (worktree / "two.txt").write_text("two\n", encoding="utf-8")
            return ProviderResult("completed", "finished", seconds=0.1)

    resumed = SequentialRunOrchestrator(
        store=store,
        project=project,
        planner=Planner(),
        provider=ResumeExecutor(),
        validate=lambda worktree: (
            "validation passed" if (Path(worktree) / "one.txt").is_file() and (Path(worktree) / "two.txt").is_file()
            else "validation failed"
        ),
        worktree_base=tmp_path / "resume-worktrees",
        auto_apply=True,
    ).resume(stopped.run_id)

    assert resumed.status == "needs_attention"
    assert resumed.integration_path and (resumed.integration_path / "one.txt").is_file()
    assert (resumed.integration_path / "two.txt").is_file()
    assert not (root / "one.txt").exists() and not (root / "two.txt").exists()
    tasks = store.list_run_tasks(stopped.run_id)
    assert any(task.status == "review_required" for task in tasks)
    assert all(task.status != "completed" for task in tasks)
    assert store.get_task_completion(tasks[0].id).unmet_criteria == (
        "Multiple unfinished tasks need separate verification",
    )


def test_ten_consecutive_fake_provider_runs_have_one_task_start_each(tmp_path) -> None:
    root, store, project = _project(tmp_path)
    events = []

    class RepeatedPlanner:
        provider = "codex"

        def __init__(self):
            self.number = 0

        def plan(self, _packet):
            self.number += 1
            return parse_plan(
                {
                    "objective": f"Run {self.number}",
                    "tasks": [{
                        "key": "change", "title": "Change", "objective": f"Create run-{self.number}.txt",
                        "risk": "low", "depends_on": [], "expected_paths": [f"run-{self.number}.txt"],
                    }],
                }
            )

    class RepeatedExecutor(Executor):
        def execute(self, worktree, prompt, *, timeout_seconds=1200):
            match = re.search(r"run-(\d+)\.txt", prompt)
            assert match
            (Path(worktree) / f"run-{match.group(1)}.txt").write_text("done\n", encoding="utf-8")
            return ProviderResult("completed", "done", seconds=0.01)

    planner = RepeatedPlanner()
    for _ in range(10):
        result = SequentialRunOrchestrator(
            store=store, project=project, planner=planner, provider=RepeatedExecutor(),
            validate=lambda _root: "validation passed", worktree_base=tmp_path / "repeat-worktrees",
            auto_apply=True, on_event=lambda kind, payload: events.append((kind, payload)),
        ).run("Repeated deterministic workflow", mode="FRONTIER_ONLY")
        assert result.status == "completed"

    assert sum(kind == "task_started" for kind, _payload in events) == 10
    assert all((root / f"run-{number}.txt").is_file() for number in range(1, 11))
