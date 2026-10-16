"""Persisted Build plans survive a process restart without implicit execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult
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
    assert window.build_resume_button.isEnabled()
    assert window.build_resume_button.text() == "Review interrupted run"
    assert window.build_open_worktree_button.isEnabled()
    replay = ScriptedExecutor("hello.txt", "complete\n")
    restarted = SequentialRunOrchestrator(
        store=reopened, project=reopened.get_project(project.id),
        planner=ScriptedPlanner("hello.txt", "complete\n"), provider=replay,
        validate=lambda _path: "validation passed",
        worktree_base=tmp_path / "worktrees", auto_apply=False,
        require_plan_approval=True,
    )
    proposed = restarted.resume(prepared.run_id)
    assert proposed.status == "awaiting_approval" and replay.calls == 0
    assert reopened.get_latest_plan_record(prepared.run_id)["source"] == "interrupted_recovery_proposed"
    assert not (Path(interrupted["worktree"]) / "hello.txt").exists()
    assert reopened.get_run(prepared.run_id)["final_commit"] == interrupted["base_commit"]
    (Path(interrupted["worktree"]) / "hello.txt").write_text("partial\n", encoding="utf-8")
    git(Path(interrupted["worktree"]), "add", "hello.txt")
    git(Path(interrupted["worktree"]), "commit", "-m", "unverified partial output")
    clean_observation = observe_interrupted_run(reopened, prepared.run_id)
    assert clean_observation["workspace_state"] == "clean_unverified"
    assert clean_observation["accepted_tasks"] == 0
    assert recover_interrupted_builds(reopened) == ()
    assert len([event for event in reopened.list_events(prepared.run_id)
                if event.kind == "run_recovery_observed"]) == 1


def test_s093_restart_preserves_first_checkpoint_and_finishes_five_task_plan(tmp_path):
    root, store, project, _executor, orchestrator = fixture(tmp_path)
    keys = ("one", "two", "three", "four", "five")

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Build five dependent files", "tasks": [
                {"key": key, "title": key, "objective": f"Write {key}.txt", "risk": "low",
                 "depends_on": [keys[index - 1]] if index else [],
                 "expected_paths": [f"{key}.txt"]}
                for index, key in enumerate(keys)
            ]})

    class Executor:
        name = "scripted"
        model = "fixture-model"

        def __init__(self, *, crash=False):
            self.crash = crash
            self.calls = []

        def execute(self, worktree, prompt, **_kwargs):
            key = next(key for key in keys if f"({key})" in prompt)
            self.calls.append(key)
            if self.crash and key == "two":
                (Path(worktree) / "two.txt").write_text("unverified\n", encoding="utf-8")
                raise SystemExit("simulated process exit")
            (Path(worktree) / f"{key}.txt").write_text(f"verified {key}\n", encoding="utf-8")
            return ProviderResult("completed", "done", seconds=0.01)

        def cancel(self):
            pass

    def validate_task(path, task):
        target = Path(path) / f"{task.key}.txt"
        return ("validation passed" if target.is_file()
                and target.read_text(encoding="utf-8") == f"verified {task.key}\n"
                else "validation failed")

    def configure(instance, executor):
        instance.planner = Planner()
        instance.provider = executor
        instance.validate_task = validate_task
        instance.validate = lambda path: (
            "validation passed" if all(
                validate_task(path, task).startswith("validation passed")
                for task in Planner().plan(None).tasks
            ) else "validation failed"
        )

    first = Executor(crash=True)
    configure(orchestrator, first)
    prepared = orchestrator.run("Build five dependent files", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"
    approve(store, prepared.run_id)
    with pytest.raises(SystemExit, match="simulated process exit"):
        orchestrator.execute_approved(prepared.run_id)
    interrupted = store.get_run(prepared.run_id)
    assert first.calls == ["one", "two"]
    original_assignments = store.get_latest_plan_record(prepared.run_id)["payload"]["task_assignments"]
    tasks = {task.key: task for task in store.list_run_tasks(prepared.run_id)}
    first_completion = store.get_task_completion(tasks["one"].id)
    assert first_completion is not None and tasks["one"].status == "completed"
    assert tasks["two"].attempts == 1 and tasks["two"].status == "running"
    assert not (root / "one.txt").exists()

    store.add_event(prepared.run_id, "run_owner", {"pid": 99999999, "birth": "dead"})
    reopened = ChatStore(tmp_path / "store.sqlite3")
    assert recover_interrupted_builds(reopened) == (prepared.run_id,)
    resumed_executor = Executor()
    restarted = SequentialRunOrchestrator(
        store=reopened, project=reopened.get_project(project.id),
        planner=Planner(), provider=resumed_executor,
        validate=lambda path: (
            "validation passed" if all(
                validate_task(path, task).startswith("validation passed")
                for task in Planner().plan(None).tasks
            ) else "validation failed"
        ), validate_task=validate_task,
        worktree_base=tmp_path / "worktrees", auto_apply=False,
        require_plan_approval=True,
    )
    proposal = restarted.resume(prepared.run_id)
    assert proposal.status == "awaiting_approval" and resumed_executor.calls == []
    assert proposal.final_commit == first_completion.revision
    assert not (Path(interrupted["worktree"]) / "two.txt").exists()
    saved = reopened.get_latest_plan_record(prepared.run_id)
    assert saved["source"] == "interrupted_recovery_proposed"
    assert saved["payload"]["task_assignments"] == original_assignments
    assert [task["depends_on"] for task in saved["payload"]["tasks"]] == [
        [], ["one"], ["two"], ["three"], ["four"],
    ]
    with pytest.raises(PermissionError):
        restarted.execute_approved(prepared.run_id)
    approve(reopened, prepared.run_id)
    result = restarted.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert resumed_executor.calls == ["two", "three", "four", "five"]
    final_tasks = {task.key: task for task in reopened.list_run_tasks(prepared.run_id)}
    assert all(task.status == "completed" for task in final_tasks.values())
    assert final_tasks["two"].attempts == 2
    assert reopened.get_task_completion(final_tasks["one"].id).revision == first_completion.revision
    assert reopened.get_latest_plan_record(prepared.run_id)["id"] == saved["id"]
    assert all(
        (result.integration_path / f"{key}.txt").read_text(encoding="utf-8") == f"verified {key}\n"
        for key in keys
    )
    assert not any((root / f"{key}.txt").exists() for key in keys)


def test_s097_interrupted_repair_preserves_attempt_history_and_requires_approval(tmp_path):
    _root, store, project, _executor, orchestrator = fixture(tmp_path, "hello.txt", "correct\n")

    class RepairCrash:
        name = "scripted"
        model = "fixture-model"

        def __init__(self):
            self.calls = 0

        def execute(self, worktree, _prompt, **_kwargs):
            self.calls += 1
            (Path(worktree) / "hello.txt").write_text(
                f"wrong {self.calls}\n", encoding="utf-8",
            )
            if self.calls == 2:
                raise SystemExit("interrupted repair")
            return ProviderResult("completed", "done", seconds=0.01)

        def cancel(self):
            pass

    crashing = RepairCrash()
    orchestrator.provider = crashing
    orchestrator.max_task_attempts = 3
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    with pytest.raises(SystemExit, match="interrupted repair"):
        orchestrator.execute_approved(prepared.run_id)
    assert crashing.calls == 2
    task = store.list_run_tasks(prepared.run_id)[0]
    assert task.attempts == 2 and task.status == "retrying"
    preserved = [event for event in store.list_events(prepared.run_id)
                 if event.kind == "failed_attempt_preserved"]
    assert len(preserved) == 1
    assert json.loads(preserved[0].payload_json)["attempt"] == 1
    store.add_event(prepared.run_id, "run_owner", {"pid": 99999999, "birth": "dead"})
    reopened = ChatStore(tmp_path / "store.sqlite3")
    assert recover_interrupted_builds(reopened) == (prepared.run_id,)
    resumed_executor = ScriptedExecutor("hello.txt", "correct\n")
    restarted = SequentialRunOrchestrator(
        store=reopened, project=reopened.get_project(project.id),
        planner=ScriptedPlanner("hello.txt", "correct\n"), provider=resumed_executor,
        validate=lambda path: (
            "validation passed" if (Path(path) / "hello.txt").read_text(encoding="utf-8") == "correct\n"
            else "validation failed"
        ), worktree_base=tmp_path / "worktrees", auto_apply=False,
        require_plan_approval=True, max_task_attempts=3,
    )
    proposal = restarted.resume(prepared.run_id)
    assert proposal.status == "awaiting_approval" and resumed_executor.calls == 0
    recovery = [event for event in reopened.list_events(prepared.run_id)
                if event.kind == "plan_recovery_proposed"]
    assert len(recovery) == 1
    assert json.loads(recovery[0].payload_json)["unverified_stash"]
    approve(reopened, prepared.run_id)
    result = restarted.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert resumed_executor.calls == 1
    assert reopened.list_run_tasks(prepared.run_id)[0].attempts == 3
    attempt_events = [json.loads(event.payload_json)["attempt"]
                      for event in reopened.list_events(prepared.run_id)
                      if event.kind == "provider_attempt_started"]
    assert attempt_events == [1, 2, 3]


def test_s098_unverified_checkpoint_blocks_replay_and_preserves_evidence(tmp_path):
    root, store, project, _executor, orchestrator = fixture(tmp_path, "hello.txt", "correct\n")

    class CrashingProvider:
        name = "scripted"
        model = "fixture-model"

        def execute(self, worktree, _prompt, **_kwargs):
            (Path(worktree) / "hello.txt").write_text("unverified\n", encoding="utf-8")
            raise SystemExit("simulated process exit")

        def cancel(self):
            pass

    orchestrator.provider = CrashingProvider()
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    with pytest.raises(SystemExit):
        orchestrator.execute_approved(prepared.run_id)
    interrupted = store.get_run(prepared.run_id)
    workspace = Path(interrupted["worktree"])
    git(workspace, "add", "hello.txt")
    git(workspace, "commit", "-m", "unverified checkpoint")
    unknown_head = git(workspace, "rev-parse", "HEAD")
    store.add_event(prepared.run_id, "run_owner", {"pid": 99999999, "birth": "dead"})
    reopened = ChatStore(tmp_path / "store.sqlite3")
    assert recover_interrupted_builds(reopened) == (prepared.run_id,)
    assert observe_interrupted_run(reopened, prepared.run_id)["workspace_state"] == "clean_unverified"
    replay = ScriptedExecutor("hello.txt", "correct\n")
    restarted = SequentialRunOrchestrator(
        store=reopened, project=reopened.get_project(project.id),
        planner=ScriptedPlanner("hello.txt", "correct\n"), provider=replay,
        validate=lambda _path: "validation passed",
        worktree_base=tmp_path / "worktrees", auto_apply=False,
        require_plan_approval=True,
    )
    blocked = restarted.resume(prepared.run_id)
    assert blocked.status == "needs_attention"
    assert "unverified commit" in blocked.error and replay.calls == 0
    assert git(workspace, "rev-parse", "HEAD") == unknown_head
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "unverified\n"
    assert not (root / "hello.txt").exists()


def test_s099_discard_removes_only_selected_managed_worktree(tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from local_model_chat.ui import main_window

    root, store, project, _executor, orchestrator = fixture(tmp_path, "hello.txt", "correct\n")
    (root / "user-note.txt").write_text("keep this\n", encoding="utf-8")
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    target = result.integration_path
    other = tmp_path / "worktrees" / "other-workspace"
    git(root, "worktree", "add", "-b", "unrelated", str(other), "HEAD")
    (other / "unrelated.txt").write_text("keep this too\n", encoding="utf-8")

    class FakeClient:
        def tags(self):
            return [{"name": "molicoder:latest"}]

    monkeypatch.setattr(main_window, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    window = main_window.MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    assert window.build_discard_button.isEnabled()
    window._discard_preserved_run()
    assert not target.exists()
    assert (root / "user-note.txt").read_text(encoding="utf-8") == "keep this\n"
    assert (other / "unrelated.txt").read_text(encoding="utf-8") == "keep this too\n"
    saved = store.get_run(prepared.run_id)
    assert saved["status"] == "failed" and saved["worktree"] == ""
