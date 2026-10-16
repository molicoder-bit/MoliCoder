"""Persisted Build plans survive a process restart without implicit execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.storage import ChatStore
from test_build_plan_approval import ScriptedExecutor, ScriptedPlanner, approve, fixture


def test_s091_pending_approval_survives_restart_without_launch(tmp_path):
    root, store, project, first_executor, orchestrator = fixture(
        tmp_path, "hello.txt", "hello\n",
    )
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"
    original_plan = store.get_latest_plan_record(prepared.run_id)
    assert original_plan is not None
    assert first_executor.calls == 0
    assert store.list_run_tasks(prepared.run_id) == []
    assert not (tmp_path / "worktrees" / f"run-{prepared.run_id}" / "workspace").exists()

    reopened = ChatStore(tmp_path / "store.sqlite3")
    reopened_project = reopened.get_project(project.id)
    assert reopened_project is not None
    persisted = reopened.get_run(prepared.run_id)
    assert persisted is not None and persisted["status"] == "awaiting_approval"
    assert reopened.get_latest_plan_record(prepared.run_id)["id"] == original_plan["id"]
    assert reopened.list_run_tasks(prepared.run_id) == []

    restarted_executor = ScriptedExecutor("hello.txt", "hello\n")
    restarted = SequentialRunOrchestrator(
        store=reopened, project=reopened_project,
        planner=ScriptedPlanner("hello.txt", "hello\n"),
        provider=restarted_executor,
        validate=lambda path: (
            "validation passed" if (Path(path) / "hello.txt").read_text(encoding="utf-8") == "hello\n"
            else "validation failed: wrong content"
        ),
        worktree_base=tmp_path / "worktrees", auto_apply=False,
        require_plan_approval=True,
    )
    with pytest.raises(PermissionError):
        restarted.execute_approved(prepared.run_id)
    assert restarted_executor.calls == 0
    approve(reopened, prepared.run_id)
    result = restarted.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert restarted_executor.calls == 1 and first_executor.calls == 0
    assert (result.integration_path / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert not (root / "hello.txt").exists()
