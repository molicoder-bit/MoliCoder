"""Kandev ownership and reconciliation under failures and repeated events."""

from __future__ import annotations

import json
from pathlib import Path

from local_model_chat.kandev.contracts import KandevTaskRef
from local_model_chat.kandev.rest import KandevTransportError
from local_model_chat.kandev.run import KandevRunOrchestrator
from test_kandev_run import _approve_and_launch, _service


def _launched(tmp_path):
    store, project, connection, api, adapter = _service(tmp_path)
    orchestrator = KandevRunOrchestrator(
        store=store, project=project, adapter=adapter,
        require_plan_approval=True,
    )
    prepared = orchestrator.run("Build parser")
    assert prepared.status == "awaiting_approval" and api.launches == 0
    running = _approve_and_launch(store, orchestrator, prepared.run_id)
    assert running.status == "running" and api.launches == 1
    return store, project, api, adapter, running.run_id


def _accepted(tmp_path):
    from test_build_plan_approval import git

    store, project, api, adapter, run_id = _launched(tmp_path)
    root = Path(project.path)
    workspace = tmp_path / "remote-worktree"
    git(root, "worktree", "add", "-b", "feature/task", str(workspace))
    (workspace / "file.txt").write_text("verified remote change\n", encoding="utf-8")
    git(workspace, "add", "file.txt")
    git(workspace, "commit", "-m", "remote change")
    api.tasks[next(iter(api.tasks))].update({
        "state": "COMPLETED", "worktree_path": str(workspace),
        "worktree_branch": "feature/task", "repository_id": "repo-1",
    })
    orchestrator = KandevRunOrchestrator(
        store=store, project=project, adapter=adapter,
        validate=lambda path: (
            "validation passed" if (Path(path) / "file.txt").read_text(encoding="utf-8") == "verified remote change\n"
            else "validation failed"
        ),
    )
    accepted = orchestrator.resume(run_id)
    assert accepted.status == "ready_to_apply"
    return store, project, api, adapter, orchestrator, run_id, workspace


def test_s080_remote_failure_requires_reconciliation_without_internal_fallback(tmp_path):
    store, project, api, adapter, run_id = _launched(tmp_path)
    api.tasks[next(iter(api.tasks))]["state"] = "FAILED"
    result = KandevRunOrchestrator(store=store, project=project, adapter=adapter).resume(run_id)
    assert result.status == "needs_attention" and api.launches == 1
    assert store.list_run_tasks(run_id)[0].status == "failed"
    assert store.get_task_completion(store.list_run_tasks(run_id)[0].id) is None
    assert any(event.kind == "kandev_failure_review_required" for event in store.list_events(run_id))
    assert store.get_run(run_id)["provider"] == "kandev"


def test_s094_disconnect_mid_build_preserves_remote_identity_and_recovers(tmp_path, monkeypatch):
    store, project, api, adapter, run_id = _launched(tmp_path)
    original_read = api.read_with_session

    def disconnected(_remote_id):
        raise KandevTransportError("connection lost")

    monkeypatch.setattr(api, "read_with_session", disconnected)
    offline = KandevRunOrchestrator(store=store, project=project, adapter=adapter).resume(run_id)
    assert offline.status == "needs_attention" and "connection failed" in offline.error
    assert store.get_run(run_id)["provider"] == "kandev" and api.launches == 1
    assert store.list_run_tasks(run_id)[0].status == "running"
    assert any(event.kind == "kandev_reconnect_required" for event in store.list_events(run_id))
    monkeypatch.setattr(api, "read_with_session", original_read)
    restored = KandevRunOrchestrator(store=store, project=project, adapter=adapter).resume(run_id)
    assert restored.status == "running" and api.launches == 1


def test_s095_duplicate_completion_and_event_cursors_do_not_duplicate_acceptance(tmp_path, monkeypatch):
    store, _project, api, adapter, orchestrator, run_id, workspace = _accepted(tmp_path)
    first_completion = store.get_task_completion(store.list_run_tasks(run_id)[0].id)
    again = orchestrator.resume(run_id)
    assert again.status == "ready_to_apply" and again.final_commit == first_completion.revision
    completions = [event for event in store.list_events(run_id) if event.kind == "task_completion_record"]
    assert len(completions) == 1
    task = store.list_run_tasks(run_id)[0]
    ref = KandevTaskRef(run_id, task.id, next(iter(api.tasks)), "remote-1")
    repeated = {"cursor": "cursor-1", "kind": "message", "payload": {"text": "completed"}}
    monkeypatch.setattr(api, "events", lambda _remote_id, _cursor: (repeated, repeated))
    assert len(adapter.subscribe_events(ref)) == 1
    assert adapter.subscribe_events(ref) == ()
    messages = [event for event in store.list_events(run_id) if event.kind == "kandev_message"]
    assert len(messages) == 1
    assert json.loads(messages[0].payload_json)["cursor"] == "cursor-1"
    assert workspace.is_dir() and api.launches == 1


def test_s096_delayed_running_status_cannot_revert_accepted_completion(tmp_path):
    store, _project, api, _adapter, orchestrator, run_id, _workspace = _accepted(tmp_path)
    task = store.list_run_tasks(run_id)[0]
    accepted = store.get_task_completion(task.id)
    api.tasks[next(iter(api.tasks))]["state"] = "IN_PROGRESS"
    stale = orchestrator.resume(run_id)
    assert stale.status == "ready_to_apply" and stale.final_commit == accepted.revision
    assert store.list_run_tasks(run_id)[0].status == "completed"
    assert store.get_run(run_id)["status"] == "ready_to_apply"
    assert len([event for event in store.list_events(run_id)
                if event.kind == "task_completion_record"]) == 1
    assert any(event.kind == "kandev_stale_status_ignored" for event in store.list_events(run_id))
