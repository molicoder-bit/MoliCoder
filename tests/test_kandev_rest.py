from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocketServer

from local_model_chat.kandev.adapter import KandevTaskAdapter
from local_model_chat.kandev.contracts import KandevConnection, KandevMode, KandevTaskRef
from local_model_chat.kandev.rest import KandevRestConfig, KandevRestTaskApi, KandevTransportError
from local_model_chat.kandev.socket_rpc import call_kandev_socket
from local_model_chat.storage import ChatStore


def _config(endpoint: str = "http://127.0.0.1:38429/mcp", version: str = "0.97.0") -> KandevRestConfig:
    return KandevRestConfig(endpoint, version, "workspace-1", "workflow-1", "step-1", "repo-1", "profile-1")


class FakeOpener:
    def __init__(self) -> None:
        self.requests = []
        self.responses = []

    def open(self, request, timeout):
        assert timeout == 5
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode("utf-8"))


def test_pinned_rest_create_lookup_and_read_use_exact_endpoint_and_identity(tmp_path) -> None:
    store = ChatStore(tmp_path / "kandev.sqlite3")
    store.record_action_authorization(
        project_id=None, action="kandev_task_create", scope="workspace-1:run:1",
        risk="external", decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    api = KandevRestTaskApi(_config(), token="secret-pat", store=store)
    fake = FakeOpener()
    fake.responses = [
        {"id": "task-1", "external_id": "run:1", "creation_complete": True, "state": "CREATED"},
        {"id": "task-1", "external_id": "run:1", "creation_complete": True, "state": "CREATED"},
        {"id": "task-1", "external_id": "run:1", "state": "IN_PROGRESS"},
    ]
    api._opener = fake

    assert api.create("run:1", "Build parser")["id"] == "task-1"
    assert api.find_by_external_id("run:1")["id"] == "task-1"
    assert api.read("task-1")["state"] == "IN_PROGRESS"
    create, lookup, read = fake.requests
    assert create.full_url == "http://127.0.0.1:38429/api/v1/tasks"
    body = json.loads(create.data)
    assert body["external_id"] == "run:1"
    assert body["repositories"] == [{"repository_id": "repo-1"}]
    assert body["executor_id"] == "exec-worktree"
    assert body["agent_profile_id"] == "profile-1"
    assert body["start_agent"] is False and body["prepare_session"] is False
    assert create.get_header("Authorization") == "Bearer secret-pat"
    assert lookup.full_url.endswith("/workspaces/workspace-1/tasks/by-external-id?external_id=run%3A1")
    assert read.full_url.endswith("/tasks/task-1")


def test_rest_transport_requires_authorization_before_lifecycle_create() -> None:
    api = KandevRestTaskApi(_config())
    fake = FakeOpener()
    fake.responses = [
        {"status": "ok", "version": "v0.97.0"},
        HTTPError("http://127.0.0.1", 404, "missing", {}, None),
    ] * 2
    api._opener = fake
    adapter = KandevTaskAdapter(
        KandevConnection(KandevMode.ATTACHED, "http://127.0.0.1:38429/mcp", "0.97.0"), api,
    )
    assert adapter.probe().health_state == "ready"
    assert fake.requests[0].full_url == "http://127.0.0.1:38429/health"
    assert fake.requests[1].get_method() == "GET"
    with pytest.raises(PermissionError, match="authorization"):
        adapter.create_task(KandevTaskRef(1, 2, "run:1"), "Build parser")


def test_rest_probe_rejects_service_version_mismatch_before_task_lookup() -> None:
    api = KandevRestTaskApi(_config())
    fake = FakeOpener()
    fake.responses = [{"status": "ok", "version": "v0.96.0"}]
    api._opener = fake
    adapter = KandevTaskAdapter(
        KandevConnection(KandevMode.ATTACHED, "http://127.0.0.1:38429/mcp", "0.97.0"), api,
    )
    with pytest.raises(KandevTransportError, match="version does not match"):
        adapter.probe()
    assert len(fake.requests) == 1


def test_rest_contract_rejects_unverified_version_and_remote_endpoints() -> None:
    with pytest.raises(ValueError, match="loopback"):
        _config("https://example.com/mcp")
    with pytest.raises(ValueError, match="loopback"):
        _config("http://127.0.0.1:38429/mcp?token=secret")
    with pytest.raises(ValueError, match="0.97.0"):
        _config(version="0.98.0")


def test_rest_404_reconciles_and_other_errors_hide_server_body() -> None:
    api = KandevRestTaskApi(_config(), token="secret-pat")
    fake = FakeOpener()
    fake.responses = [
        HTTPError("http://127.0.0.1:38429/api/v1/tasks/missing", 404, "not found", {}, None),
        HTTPError("http://127.0.0.1:38429/api/v1/tasks", 500, "secret-pat", {}, None),
    ]
    api._opener = fake
    assert api.read("missing") is None
    with pytest.raises(KandevTransportError, match="HTTP 500") as caught:
        api._request("POST", "/tasks", {"external_id": "run:1"})
    assert "secret-pat" not in str(caught.value)


def test_task_session_read_supplies_worktree_identity() -> None:
    api = KandevRestTaskApi(_config())
    fake = FakeOpener()
    fake.responses = [
        {"id": "task-1", "external_id": "run:1", "state": "COMPLETED",
         "primary_session_id": "session-1"},
        {"session": {"id": "session-1", "task_id": "task-1",
                     "worktree_path": "C:/work/task-1", "worktree_branch": "feature/task-1",
                     "base_commit_sha": "abc123", "repository_id": "repo-1"}},
    ]
    api._opener = fake
    snapshot = KandevTaskAdapter(
        KandevConnection(KandevMode.ATTACHED, _config().endpoint), api,
    ).get_task(KandevTaskRef(1, 2, "run:1", "task-1"))
    assert snapshot.state == "verifying"
    assert snapshot.worktree == "C:/work/task-1"
    assert snapshot.worktree_branch == "feature/task-1"
    assert snapshot.base_commit_sha == "abc123"
    assert snapshot.repository_id == "repo-1"
    assert fake.requests[1].full_url.endswith("/task-sessions/session-1")


def test_task_session_read_rejects_mismatched_identity() -> None:
    api = KandevRestTaskApi(_config())
    fake = FakeOpener()
    fake.responses = [
        {"id": "task-1", "primary_session_id": "session-1"},
        {"session": {"id": "session-1", "task_id": "other-task",
                     "worktree_path": "C:/work/other-task"}},
    ]
    api._opener = fake
    with pytest.raises(KandevTransportError, match="session identity differs"):
        api.read_with_session("task-1")


def test_rest_create_needs_exact_external_authorization(tmp_path) -> None:
    store = ChatStore(tmp_path / "kandev.sqlite3")
    api = KandevRestTaskApi(_config(), store=store)
    with pytest.raises(PermissionError, match="approval"):
        api.create("run:1", "Build parser")
    assert KandevRestTaskApi(_config()).capabilities() == ("create", "read", "lookup_external_id", "events", "cancel", "launch")
    with pytest.raises(PermissionError, match="authorization"):
        KandevRestTaskApi(_config()).create("run:1", "Build parser")


def test_agent_launch_requires_separate_exact_authorization(tmp_path) -> None:
    store = ChatStore(tmp_path / "kandev.sqlite3")
    api = KandevRestTaskApi(_config(), store=store)
    fake = FakeOpener()
    api._opener = fake
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.record_action_authorization(
        project_id=None, action="kandev_task_create", scope="workspace-1:run:1",
        risk="external", decision="approved", actor="user", expires_at=expiry,
    )
    with pytest.raises(PermissionError, match="approval"):
        api.create_and_launch("run:1", "Build parser")
    assert fake.requests == []

    store.record_action_authorization(
        project_id=None, action="kandev_agent_launch", scope="workspace-1:run:other:profile-1",
        risk="external", decision="approved", actor="user", expires_at=expiry,
    )
    with pytest.raises(PermissionError, match="approval"):
        api.create_and_launch("run:1", "Build parser")
    assert fake.requests == []

    store.record_action_authorization(
        project_id=None, action="kandev_agent_launch", scope="workspace-1:run:1:profile-1",
        risk="external", decision="approved", actor="user", expires_at=expiry,
    )
    fake.responses = [{
        "id": "task-1", "external_id": "run:1", "creation_complete": True,
        "state": "IN_PROGRESS", "primary_session_id": "session-1",
    }]
    result = api.create_and_launch("run:1", "Build parser")
    assert result["primary_session_id"] == "session-1"
    body = json.loads(fake.requests[0].data)
    assert body["start_agent"] is True
    assert body["external_id"] == "run:1"
    assert body["agent_profile_id"] == "profile-1"


def test_message_cursor_recovers_from_saved_event_without_replaying_content(tmp_path) -> None:
    store = ChatStore(tmp_path / "events.sqlite3")
    root = tmp_path / "project"
    root.mkdir()
    project = store.upsert_project("Project", str(root), "python")
    run_id = store.create_run(project.id, "Build parser", "HYBRID")
    task_id = store.create_run_task(run_id, "T1", "Parser", "Build parser", "low")
    api = KandevRestTaskApi(_config())
    fake = FakeOpener()
    task = {"id": "task-1", "external_id": "run:1", "state": "IN_PROGRESS", "primary_session_id": "session-1"}
    fake.responses = [
        task,
        {"messages": [{"id": "message-1", "session_id": "session-1", "task_id": "task-1",
                       "author_type": "agent", "type": "text", "content": "Authorization: Bearer secret-value",
                       "created_at": "2026-10-15T10:00:00Z"}], "has_more": False, "cursor": "message-1"},
        task,
        {"messages": [{"id": "message-2", "session_id": "session-1", "task_id": "task-1",
                       "author_type": "agent", "type": "text", "content": "done",
                       "created_at": "2026-10-15T10:01:00Z"}], "has_more": False, "cursor": "message-2"},
    ]
    api._opener = fake
    adapter = KandevTaskAdapter(
        KandevConnection(KandevMode.ATTACHED, "http://127.0.0.1:38429/mcp", "0.97.0"),
        api, store=store,
    )
    ref = KandevTaskRef(run_id, task_id, "run:1", "task-1")
    first = adapter.subscribe_events(ref)
    restarted_adapter = KandevTaskAdapter(
        KandevConnection(KandevMode.ATTACHED, "http://127.0.0.1:38429/mcp", "0.97.0"),
        api, store=ChatStore(tmp_path / "events.sqlite3"),
    )
    second = restarted_adapter.subscribe_events(ref)
    assert [event.cursor for event in first + second] == ["session-1|message-1", "session-1|message-2"]
    assert "secret-value" not in str(first[0].payload)
    assert second[0].payload["content"] == "done"
    assert fake.requests[3].full_url.endswith("&after=message-1")
    assert len([event for event in store.list_events(run_id) if event.kind == "kandev_message"]) == 2


def test_new_session_does_not_reuse_old_session_message_cursor() -> None:
    api = KandevRestTaskApi(_config())
    fake = FakeOpener()
    fake.responses = [
        {"id": "task-1", "primary_session_id": "session-2"},
        {"messages": [{"id": "message-1", "session_id": "session-2", "task_id": "task-1",
                       "content": "new session"}], "has_more": False},
    ]
    api._opener = fake
    events = api.events("task-1", "session-1|message-9")
    assert events[0]["cursor"] == "session-2|message-1"
    assert "after=" not in fake.requests[1].full_url


def test_cancel_targets_only_the_observed_execution_id() -> None:
    calls = []
    def socket_call(endpoint, token, action, payload):
        calls.append((endpoint, token, action, payload))
        return {"success": True}
    api = KandevRestTaskApi(_config(), token="pat-secret", socket_call=socket_call)
    fake = FakeOpener()
    fake.responses = [
        {"id": "task-1", "state": "IN_PROGRESS", "primary_session_id": "session-1"},
        {"session": {"id": "session-1", "task_id": "task-1", "agent_execution_id": "execution-17"}},
    ]
    api._opener = fake
    result = api.cancel("task-1")
    assert result == {"status": "stop_acknowledged", "session_id": "session-1", "agent_execution_id": "execution-17"}
    assert calls == [("http://127.0.0.1:38429/mcp", "pat-secret", "agent.stop", {"agent_id": "execution-17"})]
    assert fake.requests[1].full_url.endswith("/task-sessions/session-1")


def test_cancel_refuses_mismatched_or_missing_execution() -> None:
    calls = []
    api = KandevRestTaskApi(_config(), socket_call=lambda *_args: calls.append(True) or {"success": True})
    fake = FakeOpener()
    fake.responses = [
        {"id": "task-1", "state": "IN_PROGRESS", "primary_session_id": "session-1"},
        {"session": {"id": "session-1", "task_id": "replacement-task", "agent_execution_id": "execution-17"}},
        {"id": "task-1", "state": "IN_PROGRESS", "primary_session_id": "session-1"},
        {"session": {"id": "session-1", "task_id": "task-1"}},
    ]
    api._opener = fake
    with pytest.raises(KandevTransportError, match="identity differs"):
        api.cancel("task-1")
    with pytest.raises(KandevTransportError, match="no observed agent execution"):
        api.cancel("task-1")
    assert calls == []


def test_pinned_websocket_rpc_sends_exact_stop_envelope(qtbot) -> None:
    server = QWebSocketServer("Kandev fixture", QWebSocketServer.SslMode.NonSecureMode)
    assert server.listen(QHostAddress("127.0.0.1"), 0)
    received = []
    peers = []
    def accepted():
        peer = server.nextPendingConnection()
        peers.append(peer)
        def message(text):
            envelope = json.loads(text)
            received.append(envelope)
            peer.sendTextMessage(json.dumps({
                "id": envelope["id"], "type": "response", "action": envelope["action"],
                "payload": {"success": True},
            }))
        peer.textMessageReceived.connect(message)
    server.newConnection.connect(accepted)
    try:
        result = call_kandev_socket(
            f"http://127.0.0.1:{server.serverPort()}/mcp", "", "agent.stop", {"agent_id": "execution-17"},
        )
        assert result == {"success": True}
        assert received[0]["type"] == "request"
        assert received[0]["action"] == "agent.stop"
        assert received[0]["payload"] == {"agent_id": "execution-17"}
    finally:
        server.close()
