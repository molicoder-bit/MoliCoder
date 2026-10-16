import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from local_model_chat.kandev.adapter import KandevTaskAdapter
from local_model_chat.kandev.contracts import KandevConnection, KandevMode
from local_model_chat.kandev.run import KandevRunOrchestrator
from local_model_chat.molicoder.runtime import build_orchestrator
from local_model_chat.molicoder.plan_approval import approval_scope
from local_model_chat.storage import ChatStore


class FakeApi:
    def __init__(self):
        self.tasks = {}
        self.launches = 0
        self.config = SimpleNamespace(repository_id="repo-1")

    def capabilities(self):
        return ("create", "read", "lookup_external_id", "events", "cancel", "launch")

    def create_and_launch(self, external_id, objective):
        self.launches += 1
        task = {"id": "remote-1", "external_id": external_id, "state": "IN_PROGRESS",
                "primary_session_id": "session-1", "creation_complete": True}
        self.tasks[external_id] = task
        return task

    def find_by_external_id(self, external_id):
        return self.tasks.get(external_id)

    def read(self, remote_task_id):
        return next((task for task in self.tasks.values() if task["id"] == remote_task_id), None)

    def read_with_session(self, remote_task_id):
        return self.read(remote_task_id)

    def events(self, remote_task_id, after_cursor):
        return ()

    def cancel(self, remote_task_id):
        return {"status": "requested"}


def _service(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, check=True).stdout.strip()
    git("init", "-b", "main")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "Fixture")
    (root / "file.txt").write_text("base", encoding="utf-8")
    git("add", "file.txt")
    git("commit", "-m", "base")
    store = ChatStore(tmp_path / "state.sqlite3")
    project = store.upsert_project("Project", str(root), "python")
    connection = KandevConnection(KandevMode.ATTACHED, "http://127.0.0.1:38429/mcp")
    api = FakeApi()
    adapter = KandevTaskAdapter(connection, api, store=store)
    return store, project, connection, api, adapter


def _approve_and_launch(store, orchestrator, run_id):
    run = store.get_run(run_id)
    plan = store.get_latest_plan_record(run_id)
    assert run["status"] == "awaiting_approval" and plan is not None
    store.record_action_authorization(
        project_id=run["project_id"], action="build_plan_execute",
        scope=approval_scope(run, plan["id"], plan["payload"]), risk="mutation",
        decision="approved", actor="scenario_harness",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    return orchestrator.execute_approved(run_id)


def test_selected_build_is_bound_to_kandev_before_dispatch_and_recovers(tmp_path):
    store, project, connection, api, adapter = _service(tmp_path)
    orchestrator = build_orchestrator(
        store=store, project=project, kandev_connection=connection, kandev_adapter=adapter,
    )
    assert isinstance(orchestrator, KandevRunOrchestrator)
    prepared = orchestrator.run("Build parser")
    assert prepared.status == "awaiting_approval" and api.launches == 0
    with pytest.raises(PermissionError):
        orchestrator.execute_approved(prepared.run_id)
    result = _approve_and_launch(store, orchestrator, prepared.run_id)
    assert result.status == "running" and api.launches == 1
    run = store.get_run(result.run_id)
    assert run["provider"] == "kandev"
    selection = [event for event in store.list_events(result.run_id) if event.kind == "kandev_owner_selected"]
    assert len(selection) == 1

    restarted = KandevRunOrchestrator(
        store=ChatStore(tmp_path / "state.sqlite3"), project=project,
        adapter=KandevTaskAdapter(connection, api, store=store),
    )
    observed = restarted.resume(result.run_id)
    assert observed.status == "running" and api.launches == 1
    api.tasks[next(iter(api.tasks))]["state"] = "COMPLETED"
    completed_remotely = restarted.resume(result.run_id)
    assert completed_remotely.status == "needs_attention"
    assert store.list_run_tasks(result.run_id)[0].status == "verifying"


def test_kandev_launch_authorizer_receives_persisted_owner_before_dispatch(tmp_path):
    store, project, connection, api, adapter = _service(tmp_path)
    observed = []

    def authorize(ref):
        run = store.get_run(ref.run_id)
        events = store.list_events(ref.run_id)
        observed.append((run["provider"], ref.external_id, events[-1].kind))

    orchestrator = build_orchestrator(
        store=store, project=project, kandev_connection=connection,
        kandev_adapter=adapter, kandev_authorize_launch=authorize,
    )
    prepared = orchestrator.run("Build parser")
    result = _approve_and_launch(store, orchestrator, prepared.run_id)
    assert result.status == "running" and api.launches == 1
    assert observed == [(
        "kandev", f"molicoder:project:{project.id}:run:{result.run_id}:task:1",
        "kandev_owner_selected",
    )]


def test_kandev_denied_launch_is_not_reported_as_uncertain_remote_create(tmp_path):
    store, project, connection, api, adapter = _service(tmp_path)
    def deny(_ref):
        raise PermissionError("agent launch authorization was revoked")
    orchestrator = build_orchestrator(
        store=store, project=project, kandev_connection=connection,
        kandev_adapter=adapter, kandev_authorize_launch=deny,
    )
    prepared = orchestrator.run("Build parser")
    result = _approve_and_launch(store, orchestrator, prepared.run_id)
    assert result.status == "needs_attention" and api.launches == 0
    assert store.list_run_tasks(result.run_id)[0].status == "waiting_approval"
    assert any(event.kind == "kandev_launch_blocked" for event in store.list_events(result.run_id))
    assert not any(event.kind == "kandev_reconciliation_required" for event in store.list_events(result.run_id))


def test_missing_remote_task_keeps_kandev_ownership_and_requires_review(tmp_path):
    store, project, connection, api, adapter = _service(tmp_path)
    orchestrator = build_orchestrator(
        store=store, project=project, kandev_connection=connection, kandev_adapter=adapter,
    )
    prepared = orchestrator.run("Build parser")
    result = _approve_and_launch(store, orchestrator, prepared.run_id)
    api.tasks.clear()
    observed = KandevRunOrchestrator(store=store, project=project, adapter=adapter).resume(result.run_id)
    assert observed.status == "needs_attention"
    assert store.list_run_tasks(result.run_id)[0].status == "review_required"
    assert api.launches == 1


def test_remote_completion_records_only_a_verified_git_worktree(tmp_path):
    store, project, connection, api, adapter = _service(tmp_path)
    root = tmp_path / "project"
    def git(path, *args):
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                              text=True, check=True).stdout.strip()
    base = git(root, "rev-parse", "HEAD")
    worktree = tmp_path / "remote-worktree"
    git(root, "worktree", "add", "-b", "feature/task", str(worktree))
    (worktree / "file.txt").write_text("changed", encoding="utf-8")
    git(worktree, "add", "file.txt")
    git(worktree, "commit", "-m", "task")

    orchestrator = build_orchestrator(
        store=store, project=project, kandev_connection=connection, kandev_adapter=adapter,
    )
    prepared = orchestrator.run("Build parser")
    result = _approve_and_launch(store, orchestrator, prepared.run_id)
    task = next(iter(api.tasks.values()))
    task.update({"state": "COMPLETED", "worktree_path": str(worktree),
                 "worktree_branch": "feature/task",
                 "repository_id": "repo-1"})
    reviewed = KandevRunOrchestrator(store=store, project=project, adapter=adapter).resume(result.run_id)
    assert reviewed.status == "needs_attention"
    assert store.get_run(result.run_id)["final_commit"] == git(worktree, "rev-parse", "HEAD")
    assert store.list_run_tasks(result.run_id)[0].worktree == str(worktree.resolve())
    assert any(event.kind == "kandev_worktree_reconciled" for event in store.list_events(result.run_id))

    failed = KandevRunOrchestrator(
        store=store, project=project, adapter=adapter,
        validate=lambda _path: "validation failed: fixture",
    ).resume(result.run_id)
    assert failed.status == "needs_attention"
    assert store.get_task_completion(store.list_run_tasks(result.run_id)[0].id) is None

    accepted = KandevRunOrchestrator(
        store=store, project=project, adapter=adapter,
        validate=lambda _path: "validation passed: fixture",
    ).resume(result.run_id)
    assert accepted.status == "ready_to_apply" and accepted.completed_tasks == 1
    task_record = store.list_run_tasks(result.run_id)[0]
    assert task_record.status == "completed"
    assert task_record.changed_paths == ("file.txt",)
    assert store.get_task_completion(task_record.id).revision == git(worktree, "rev-parse", "HEAD")

    (root / "uncommitted.txt").write_text("user edit", encoding="utf-8")
    with pytest.raises(ValueError, match="dirty"):
        KandevRunOrchestrator(store=store, project=project, adapter=adapter).apply(result.run_id)
    (root / "uncommitted.txt").unlink()
    applied = KandevRunOrchestrator(store=store, project=project, adapter=adapter).apply(result.run_id)
    assert applied.status == "completed"
    assert git(root, "rev-parse", "HEAD") == accepted.final_commit
    again = KandevRunOrchestrator(store=store, project=project, adapter=adapter).apply(result.run_id)
    assert again.status == "completed"
    assert KandevRunOrchestrator(store=store, project=project, adapter=adapter).resume(result.run_id).status == "completed"
    assert len([event for event in store.list_events(result.run_id)
                if event.kind == "kandev_changes_applied"]) == 1
