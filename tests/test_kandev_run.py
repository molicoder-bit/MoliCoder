import subprocess
from types import SimpleNamespace

import pytest

from local_model_chat.kandev.adapter import KandevTaskAdapter
from local_model_chat.kandev.contracts import KandevConnection, KandevMode
from local_model_chat.kandev.run import KandevRunOrchestrator
from local_model_chat.molicoder.runtime import build_orchestrator
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


def test_selected_build_is_bound_to_kandev_before_dispatch_and_recovers(tmp_path):
    store, project, connection, api, adapter = _service(tmp_path)
    orchestrator = build_orchestrator(
        store=store, project=project, kandev_connection=connection, kandev_adapter=adapter,
    )
    assert isinstance(orchestrator, KandevRunOrchestrator)
    result = orchestrator.run("Build parser")
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


def test_missing_remote_task_keeps_kandev_ownership_and_requires_review(tmp_path):
    store, project, connection, api, adapter = _service(tmp_path)
    result = build_orchestrator(
        store=store, project=project, kandev_connection=connection, kandev_adapter=adapter,
    ).run("Build parser")
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

    result = build_orchestrator(
        store=store, project=project, kandev_connection=connection, kandev_adapter=adapter,
    ).run("Build parser")
    task = next(iter(api.tasks.values()))
    task.update({"state": "COMPLETED", "worktree_path": str(worktree),
                 "worktree_branch": "feature/task", "base_commit_sha": base,
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
