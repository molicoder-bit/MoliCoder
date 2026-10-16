from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.molicoder.plan_approval import approval_scope
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.storage import ChatStore


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


class ScriptedPlanner:
    provider = "scripted"

    def __init__(self, filename: str, content: str) -> None:
        self.filename, self.content = filename, content

    def plan(self, _packet):
        return parse_plan({"objective": f"Write {self.filename}", "tasks": [{
            "key": "write", "title": f"Write {self.filename}",
            "objective": f"Write exact content to {self.filename}",
            "risk": "low", "depends_on": [], "expected_paths": [self.filename],
            "acceptance_criteria": ["Configured validation must pass"],
            "verification_checks": ["project-default"],
        }]})


class ScriptedExecutor:
    name = "scripted"
    model = "fixture-model"

    def __init__(self, filename: str, content: str) -> None:
        self.filename, self.content, self.calls = filename, content, 0

    def execute(self, worktree, _prompt, *, timeout_seconds=1200):
        self.calls += 1
        (Path(worktree) / self.filename).write_text(self.content, encoding="utf-8")
        return ProviderResult("completed", "done", seconds=0.01)

    def cancel(self):
        pass


def fixture(tmp_path: Path, filename="hello.txt", content=""):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / ".gitignore").write_text(".molicoder/\n", encoding="utf-8")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    store = ChatStore(tmp_path / "store.sqlite3")
    project = ProjectRegistry(store).add(root)
    executor = ScriptedExecutor(filename, content)

    def validate(path):
        return ("validation passed" if (Path(path) / filename).is_file()
                and (Path(path) / filename).read_text(encoding="utf-8") == content
                else "validation failed: wrong file content")

    orchestrator = SequentialRunOrchestrator(
        store=store, project=project, planner=ScriptedPlanner(filename, content),
        provider=executor, validate=validate,
        worktree_base=tmp_path / "worktrees", auto_apply=False,
        require_plan_approval=True,
    )
    return root, store, project, executor, orchestrator


def approve(store, run_id):
    run = store.get_run(run_id)
    plan = store.get_latest_plan_record(run_id)
    store.record_action_authorization(
        project_id=run["project_id"], action="build_plan_execute",
        scope=approval_scope(run, plan["id"], plan["payload"]), risk="mutation",
        decision="approved", actor="scenario_harness",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )


def test_s033_rejected_plan_is_persisted_without_execution(tmp_path, qtbot, monkeypatch):
    from local_model_chat.ui.main_window import MainWindow

    root, store, project, executor, orchestrator = fixture(tmp_path, "hello.txt", "hello\n")
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"

    class FakeClient:
        def tags(self):
            return [{"name": "molicoder:latest"}]

    window = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(window, "_plan_review_decision", lambda *_args, **_kwargs: "reject")
    window._review_build_plan(prepared.run_id)

    plan = store.get_latest_plan_record(prepared.run_id)
    run = store.get_run(prepared.run_id)
    decision = store.latest_action_authorization(
        project_id=project.id, action="build_plan_execute",
        scope=approval_scope(run, plan["id"], plan["payload"]),
    )
    assert decision["decision"] == "denied"
    assert plan["payload"]["tasks"][0]["key"] == "write"
    assert executor.calls == 0
    assert not (root / "hello.txt").exists()
    with pytest.raises(PermissionError):
        orchestrator.execute_approved(prepared.run_id)


@pytest.mark.parametrize("filename,content", [("hello.txt", ""), ("greeting.txt", "Hello World\n")])
def test_s001_s002_plan_approval_and_exact_output(tmp_path, filename, content):
    root, store, _project, executor, orchestrator = fixture(tmp_path, filename, content)
    prepared = orchestrator.run(f"Write {filename}", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"
    planned = store.get_latest_plan_record(prepared.run_id)["payload"]
    assert len(planned["tasks"]) == 1 and planned["tasks"][0]["depends_on"] == []
    assignment = planned["task_assignments"]["write"]
    assert assignment["runner"] == "provider_cli"
    assert assignment["provider"] == "scripted" and assignment["model"] == "fixture-model"
    assert executor.calls == 0
    assert not (root / filename).exists()
    assert not (tmp_path / "worktrees" / f"run-{prepared.run_id}" / "workspace").exists()
    with pytest.raises(PermissionError):
        orchestrator.execute_approved(prepared.run_id)
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert executor.calls == 1
    assert result.integration_path and (result.integration_path / filename).read_text(encoding="utf-8") == content
    assert not (root / filename).exists()
    task = store.list_run_tasks(prepared.run_id)[0]
    assert task.status == "completed"
    assert store.get_task_completion(task.id).validation_summary.startswith("validation passed")


def test_s031_s039_stale_plan_revision_cannot_execute(tmp_path):
    _root, store, _project, executor, orchestrator = fixture(tmp_path)
    prepared = orchestrator.run("Create hello", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    original = store.get_latest_plan_record(prepared.run_id)
    store.save_plan(prepared.run_id, "edited", "scripted", original["payload"])
    with pytest.raises(PermissionError):
        orchestrator.execute_approved(prepared.run_id)
    assert executor.calls == 0


def test_assignment_change_requires_supported_replanning(tmp_path):
    _root, store, _project, executor, orchestrator = fixture(tmp_path)
    prepared = orchestrator.run("Create hello", mode="FRONTIER_ONLY")
    record = store.get_latest_plan_record(prepared.run_id)
    changed = record["payload"]
    changed["task_assignments"]["write"]["model"] = "unsupported-model"
    store.save_plan(prepared.run_id, "edited", "scripted", changed)
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "needs_attention" and "assignments changed" in result.error
    assert executor.calls == 0


def test_project_head_change_blocks_approved_execution(tmp_path):
    root, store, _project, executor, orchestrator = fixture(tmp_path)
    prepared = orchestrator.run("Create hello", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    (root / "unrelated.txt").write_text("new\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "other")
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "needs_attention"
    assert "HEAD changed" in result.error
    assert executor.calls == 0
    assert not (root / "hello.txt").exists()


def test_s107_plan_revision_change_during_execution_stops_next_task(tmp_path):
    root, store, _project, _executor, orchestrator = fixture(tmp_path)

    class TwoTaskPlanner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Two outputs", "tasks": [
                {"key": "one", "title": "One", "objective": "Write first",
                 "risk": "low", "depends_on": [], "expected_paths": ["one.txt"]},
                {"key": "two", "title": "Two", "objective": "Write second",
                 "risk": "low", "depends_on": ["one"], "expected_paths": ["two.txt"]},
            ]})

    class EditingExecutor(ScriptedExecutor):
        def execute(self, worktree, prompt, *, timeout_seconds=1200):
            self.calls += 1
            if "(one)" in prompt:
                (Path(worktree) / "one.txt").write_text("one\n", encoding="utf-8")
                old = store.get_latest_plan_record(orchestrator._active_run_id)
                store.save_plan(orchestrator._active_run_id, "edited", "scripted", old["payload"])
            else:
                (Path(worktree) / "two.txt").write_text("two\n", encoding="utf-8")
            return ProviderResult("completed", "done", seconds=0.01)

    executor = EditingExecutor("", "")
    orchestrator.planner = TwoTaskPlanner()
    orchestrator.provider = executor
    orchestrator.validate_task = lambda path, task: (
        "validation passed" if (Path(path) / f"{task.key}.txt").is_file() else "validation failed"
    )
    orchestrator.validate = lambda path: (
        "validation passed" if (Path(path) / "one.txt").is_file()
        and (Path(path) / "two.txt").is_file() else "validation failed"
    )
    prepared = orchestrator.run("Write two files", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "needs_attention" and "Plan approval changed" in result.error
    assert executor.calls == 1
    assert not (root / "one.txt").exists() and not (root / "two.txt").exists()


def test_s107_plan_revision_during_last_task_stops_apply_even_if_new_revision_is_approved(tmp_path):
    root, store, _project, _executor, orchestrator = fixture(tmp_path, "hello.txt", "hello\n")
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)

    class RevisingExecutor(ScriptedExecutor):
        def execute(self, worktree, prompt, *, timeout_seconds=1200):
            result = super().execute(worktree, prompt, timeout_seconds=timeout_seconds)
            current = store.get_latest_plan_record(prepared.run_id)
            store.save_plan(prepared.run_id, "new_revision", "scripted", current["payload"])
            approve(store, prepared.run_id)
            return result

    executor = RevisingExecutor("hello.txt", "hello\n")
    orchestrator.provider = executor
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "needs_attention"
    assert "Plan approval changed before apply" in result.error
    assert executor.calls == 1
    assert not (root / "hello.txt").exists()
