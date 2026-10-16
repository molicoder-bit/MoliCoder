"""A bounded existing-file edit should take the eligible local route."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_model_chat.molicoder.executor import ExecutionResult
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.repair import RepairOutcome
from local_model_chat.molicoder.providers import ProviderResult, ProviderStatus, ProviderUnavailable
from test_build_plan_approval import approve, fixture, git


def test_s041_s043_simple_existing_file_uses_local_executor(tmp_path):
    root, store, _project, provider, orchestrator = fixture(tmp_path)
    (root / "greeting.txt").write_text("old\n", encoding="utf-8")
    git(root, "add", "greeting.txt")
    git(root, "commit", "-m", "existing greeting")

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Change greeting", "tasks": [{
                "key": "edit", "title": "Edit greeting", "objective": "Change greeting in greeting.txt",
                "risk": "low", "depends_on": [], "expected_paths": ["greeting.txt"],
                "acceptance_criteria": ["Configured validation must pass"],
                "verification_checks": ["project-default"],
                "artifact_checks": [{"kind": "file_equals", "path": "greeting.txt", "content": "new\n"}],
            }]})

    local_calls = []

    def local_executor(worktree, _prompt):
        local_calls.append(Path(worktree))
        (Path(worktree) / "greeting.txt").write_text("new\n", encoding="utf-8")
        execution = ExecutionResult("completed", "done", 1, 1)
        return RepairOutcome("completed", 1, execution, (), "", (execution,))

    orchestrator.planner = Planner()
    orchestrator.local_executor = local_executor
    orchestrator.validate = lambda path: (
        "validation passed" if (Path(path) / "greeting.txt").read_text(encoding="utf-8") == "new\n"
        else "validation failed: greeting differs"
    )
    orchestrator.validate_task = lambda path, _task: orchestrator.validate(path)
    prepared = orchestrator.run("Change greeting in greeting.txt", mode="HYBRID")
    assert prepared.status == "awaiting_approval"
    plan = store.get_latest_plan_record(prepared.run_id)["payload"]
    assert plan["task_assignments"]["edit"]["runner"] == "local"
    assert provider.calls == 0 and local_calls == []

    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert len(local_calls) == 1 and provider.calls == 0
    assert (result.integration_path / "greeting.txt").read_text(encoding="utf-8") == "new\n"
    assert (root / "greeting.txt").read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("scenario,message,task_status", [
    ("S044", "service unavailable", "needs_attention"),
    ("S045", "authentication required", "waiting_auth"),
])
def test_s044_s045_provider_failure_stops_without_edits(tmp_path, scenario, message, task_status):
    root, store, _project, _provider, orchestrator = fixture(tmp_path, "hello.txt", "hello\n")

    class UnavailableProvider:
        name = "scripted"
        model = "fixture-model"

        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            raise ProviderUnavailable(message)

        def cancel(self):
            pass

    provider = UnavailableProvider()
    orchestrator.provider = provider
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "needs_attention"
    assert message in result.error
    assert provider.calls == 1
    assert store.list_run_tasks(prepared.run_id)[0].status == task_status
    assert not (root / "hello.txt").exists()


def test_s049_recoverable_failure_uses_approved_fallback_within_attempt_cap(tmp_path):
    root, store, _project, _provider, orchestrator = fixture(tmp_path, "hello.txt", "hello\n")
    events = []
    orchestrator.on_event = lambda kind, payload: events.append((kind, payload))
    orchestrator.max_task_attempts = 2

    class Primary:
        name = "primary"
        model = "fixture-primary"

        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return ProviderResult("needs_attention", "", error="quota exceeded")

        def cancel(self):
            pass

    class Fallback:
        name = "fallback"
        model = "fixture-fallback"

        def __init__(self):
            self.calls = 0
            self.probes = 0

        def probe(self):
            self.probes += 1
            return ProviderStatus(self.name, True, True, auth_verified=True)

        def execute(self, worktree, _prompt, **_kwargs):
            self.calls += 1
            (Path(worktree) / "hello.txt").write_text("hello\n", encoding="utf-8")
            return ProviderResult("completed", "done")

        def cancel(self):
            pass

    primary, fallback = Primary(), Fallback()
    orchestrator.provider = primary
    orchestrator.alternate_provider = fallback
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"
    plan = store.get_latest_plan_record(prepared.run_id)["payload"]
    assert plan["runner_policy"]["fallback_provider"] == "fallback"
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert primary.calls == fallback.calls == fallback.probes == 1
    assert any(kind == "model_handoff" and payload["provider"] == "fallback" for kind, payload in events)
    assert (result.integration_path / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert not (root / "hello.txt").exists()


def test_s050_opaque_cli_usage_keeps_unknown_cost(tmp_path):
    root, store, _project, _provider, orchestrator = fixture(tmp_path, "hello.txt", "hello\n")

    class OpaqueProvider:
        name = "codex"
        model = "subscription-cli"
        enforcement_class = "opaque_agent"
        calls = 0

        def execute(self, worktree, _prompt, **_kwargs):
            self.calls += 1
            (Path(worktree) / "hello.txt").write_text("hello\n", encoding="utf-8")
            return ProviderResult("completed", "done", seconds=0.02)

        def cancel(self):
            pass

    provider = OpaqueProvider()
    orchestrator.provider = provider
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    usage = store.run_inference_usage(prepared.run_id)
    assert provider.calls == 1
    assert usage["opaque_cli_calls"] == 1
    assert usage["opaque_cli_cost_accuracy"] == "unknown"
    assert usage["local_estimated_cost_usd"] is None
    assert usage["local_actual_cost_usd"] is None
    assert (result.integration_path / "hello.txt").is_file()
    assert not (root / "hello.txt").exists()
