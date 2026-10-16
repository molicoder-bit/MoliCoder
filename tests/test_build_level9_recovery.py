"""Persisted Build plans survive a process restart without implicit execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.molicoder.recovery import observe_interrupted_run, recover_interrupted_builds
from local_model_chat.storage import ChatStore
from test_build_plan_approval import ScriptedExecutor, ScriptedPlanner, approve, fixture, git


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


def test_s092_interrupted_task_reconciles_dirty_worktree_without_claiming_success(tmp_path, qtbot):
    from local_model_chat.ui.main_window import MainWindow

    root, store, project, _old_executor, orchestrator = fixture(
        tmp_path, "hello.txt", "complete\n",
    )

    class CrashingProvider:
        name = "scripted"
        model = "fixture-model"

        def execute(self, worktree, _prompt, *, timeout_seconds=1200):
            (Path(worktree) / "hello.txt").write_text("partial\n", encoding="utf-8")
            raise SystemExit("simulated process exit")

        def cancel(self):
            pass

    orchestrator.provider = CrashingProvider()
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    with pytest.raises(SystemExit, match="simulated process exit"):
        orchestrator.execute_approved(prepared.run_id)
    interrupted = store.get_run(prepared.run_id)
    assert interrupted["status"] == "running"
    assert store.list_run_tasks(prepared.run_id)[0].status == "running"
    assert (Path(interrupted["worktree"]) / "hello.txt").read_text(encoding="utf-8") == "partial\n"
    assert not (root / "hello.txt").exists()
    assert store.get_task_completion(store.list_run_tasks(prepared.run_id)[0].id) is None

    # The injected fatal exit leaves this pytest process alive; replace its
    # ownership observation with a known absent PID before simulating restart.
    store.add_event(prepared.run_id, "run_owner", {"pid": 99999999, "birth": "dead"})
    reopened = ChatStore(tmp_path / "store.sqlite3")
    assert recover_interrupted_builds(reopened) == (prepared.run_id,)
    recovered = reopened.get_run(prepared.run_id)
    assert recovered["status"] == "needs_attention"
    assert reopened.list_run_tasks(prepared.run_id)[0].status == "review_required"
    observations = [event for event in reopened.list_events(prepared.run_id)
                    if event.kind == "run_recovery_observed"]
    assert len(observations) == 1
    observed = json.loads(observations[0].payload_json)
    assert observed["workspace_state"] == "dirty"
    assert observed["accepted_tasks"] == 0
    assert observed["checkpoint"] == interrupted["base_commit"]
    class FakeClient:
        def tags(self):
            return [{"name": "molicoder:latest"}]

    window = MainWindow(reopened, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    assert "worktree dirty" in window.build_ownership_status.text()
    assert not window.build_resume_button.isEnabled()
    assert window.build_open_worktree_button.isEnabled()
    replay = ScriptedExecutor("hello.txt", "complete\n")
    restarted = SequentialRunOrchestrator(
        store=reopened, project=reopened.get_project(project.id),
        planner=ScriptedPlanner("hello.txt", "complete\n"), provider=replay,
        validate=lambda _path: "validation passed",
        worktree_base=tmp_path / "worktrees", auto_apply=False,
        require_plan_approval=True,
    )
    blocked = restarted.resume(prepared.run_id)
    assert blocked.status == "needs_attention"
    assert "checkpoint review" in blocked.error and replay.calls == 0
    git(Path(interrupted["worktree"]), "add", "hello.txt")
    git(Path(interrupted["worktree"]), "commit", "-m", "unverified partial output")
    clean_observation = observe_interrupted_run(reopened, prepared.run_id)
    assert clean_observation["workspace_state"] == "clean_unverified"
    assert clean_observation["accepted_tasks"] == 0
    assert recover_interrupted_builds(reopened) == ()
    assert len([event for event in reopened.list_events(prepared.run_id)
                if event.kind == "run_recovery_observed"]) == 1
