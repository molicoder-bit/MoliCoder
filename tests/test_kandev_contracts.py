import pytest

from local_model_chat.kandev.contracts import (
    KandevAdapter,
    KandevConnection,
    KandevEvent,
    KandevMode,
    KandevTaskRef,
    KandevTaskSnapshot,
)
from local_model_chat.molicoder.providers import ProviderUnavailable
from local_model_chat.molicoder.runtime import build_orchestrator
from local_model_chat.storage import ChatStore


class LocalClient:
    def tags(self):
        return [{"name": "molicoder:latest"}]


def test_disabled_mode_uses_internal_build_without_kandev(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = ChatStore(tmp_path / "state.sqlite3")
    project = store.upsert_project("Project", str(root), "python")

    orchestrator = build_orchestrator(
        store=store, project=project, client=LocalClient(), mode="LOCAL_OFFLINE",
        kandev_connection=KandevConnection(),
    )

    assert orchestrator.project.id == project.id
    assert orchestrator.route_decision["selected"].startswith("ollama-local:")


def test_non_disabled_mode_cannot_start_before_adapter_readiness(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = ChatStore(tmp_path / "state.sqlite3")
    project = store.upsert_project("Project", str(root), "python")
    for mode in (KandevMode.ATTACHED, KandevMode.MANAGED):
        connection = KandevConnection(mode=mode, endpoint="http://127.0.0.1:8765/mcp")
        with pytest.raises(ProviderUnavailable, match="adapter readiness"):
            build_orchestrator(
                store=store, project=project, client=LocalClient(), mode="LOCAL_OFFLINE",
                kandev_connection=connection,
            )
    assert store.list_project_runs(project.id) == []


def test_task_and_event_contract_preserve_stable_external_identity() -> None:
    ref = KandevTaskRef(7, 12, "molicoder-run-7-task-12", "remote-123")
    snapshot = KandevTaskSnapshot(ref, "reconciliation_required", "2026-10-15T12:00:00Z",
                                  session_id="session-8", worktree="/tmp/work", event_cursor="cursor-5")
    event = KandevEvent(ref, "cursor-6", "task_updated", "2026-10-15T12:01:00Z", {"state": "running"})

    assert snapshot.ref.external_id == event.ref.external_id
    assert snapshot.event_cursor != event.cursor
    assert "reconcile" in KandevAdapter.__dict__
    with pytest.raises(ValueError, match="requires an endpoint"):
        KandevConnection(mode=KandevMode.ATTACHED)
