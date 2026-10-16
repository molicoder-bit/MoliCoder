"""Dependency behavior in the active, approval-gated internal executor."""

from __future__ import annotations

from pathlib import Path

from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult
from test_build_plan_approval import approve, fixture


def test_s082_successor_starts_after_parent_verification(tmp_path):
    root, store, _project, _provider, orchestrator = fixture(tmp_path)

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Build a dependent pair", "tasks": [
                {"key": "child", "title": "Child", "objective": "Write child.txt",
                 "risk": "low", "depends_on": ["parent"], "expected_paths": ["child.txt"]},
                {"key": "parent", "title": "Parent", "objective": "Write parent.txt",
                 "risk": "low", "depends_on": [], "expected_paths": ["parent.txt"]},
            ]})

    class Executor:
        name = "scripted"
        model = "fixture-model"

        def __init__(self):
            self.calls = []

        def execute(self, worktree, prompt, **_kwargs):
            if "(parent)" in prompt:
                self.calls.append("parent")
                (Path(worktree) / "parent.txt").write_text("passed parent\n", encoding="utf-8")
            elif "(child)" in prompt:
                self.calls.append("child")
                assert (Path(worktree) / "parent.txt").read_text(encoding="utf-8") == "passed parent\n"
                tasks = {task.key: task for task in store.list_run_tasks(orchestrator._active_run_id)}
                assert tasks["parent"].status == "completed"
                assert store.get_task_completion(tasks["parent"].id) is not None
                (Path(worktree) / "child.txt").write_text("passed child\n", encoding="utf-8")
            else:
                raise AssertionError("unrecognized task prompt")
            return ProviderResult("completed", "Done", seconds=0.01)

        def cancel(self):
            pass

    def validate_task(path, task):
        expected = "passed parent\n" if task.key == "parent" else "passed child\n"
        name = "parent.txt" if task.key == "parent" else "child.txt"
        target = Path(path) / name
        return "validation passed" if target.is_file() and target.read_text(encoding="utf-8") == expected else "validation failed"

    executor = Executor()
    orchestrator.planner = Planner()
    orchestrator.provider = executor
    orchestrator.validate_task = validate_task
    orchestrator.validate = lambda path: (
        "validation passed" if all(
            validate_task(path, task).startswith("validation passed")
            for task in Planner().plan(None).tasks
        ) else "validation failed"
    )
    events = []
    orchestrator.on_event = lambda kind, payload: events.append((kind, payload))
    prepared = orchestrator.run("Build dependent files", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval" and executor.calls == []
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert executor.calls == ["parent", "child"]
    ordered = [(kind, payload.get("key")) for kind, payload in events
               if kind in {"task_started", "task_completed"}]
    assert ordered == [
        ("task_started", "parent"), ("task_completed", "parent"),
        ("task_started", "child"), ("task_completed", "child"),
    ]
    assert not (root / "parent.txt").exists() and not (root / "child.txt").exists()
