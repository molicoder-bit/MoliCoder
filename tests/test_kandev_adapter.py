import pytest

from local_model_chat.kandev.adapter import KandevTaskAdapter
from local_model_chat.kandev.contracts import KandevConnection, KandevMode, KandevTaskRef
from local_model_chat.kandev.rest import KandevTransportError
from local_model_chat.storage import ChatStore


class FakeApi:
    def __init__(self):
        self.tasks = {}
        self.created = 0
        self.cancel_requested = False

    def capabilities(self):
        return ("create", "read", "lookup_external_id", "events", "cancel", "launch")

    def create(self, external_id, objective):
        if external_id not in self.tasks:
            self.created += 1
            self.tasks[external_id] = {
                "id": "remote-7", "external_id": external_id, "state": "IN_PROGRESS",
                "session_id": "session-1", "worktree_path": "/work/remote-7",
                "creation_complete": True,
            }
        return self.tasks[external_id]

    def create_and_launch(self, external_id, objective):
        return self.create(external_id, objective)

    def read(self, remote_task_id):
        return next((task for task in self.tasks.values() if task["id"] == remote_task_id), None)

    def read_with_session(self, remote_task_id):
        return self.read(remote_task_id)

    def find_by_external_id(self, external_id):
        return self.tasks.get(external_id)

    def events(self, remote_task_id, after_cursor):
        return () if after_cursor == "cursor-1" else ({
            "cursor": "cursor-1", "kind": "task_updated", "payload": {"state": "IN_PROGRESS"},
        },)

    def cancel(self, remote_task_id):
        self.cancel_requested = True
        return {"status": "requested"}


def _adapter(store=None, api=None):
    return KandevTaskAdapter(
        KandevConnection(mode=KandevMode.ATTACHED, endpoint="http://127.0.0.1:38429/mcp"),
        api or FakeApi(), store=store,
    )


def test_create_deduplicates_and_reconciles_by_external_id() -> None:
    api = FakeApi()
    adapter = _adapter(api=api)
    ref = KandevTaskRef(7, 12, "molicoder-run-7-task-12")
    first = adapter.create_task(ref, "Implement parser")
    second = adapter.create_task(ref, "Implement parser")

    assert api.created == 1
    assert first.ref.remote_task_id == second.ref.remote_task_id == "remote-7"
    assert first.state == "running"
    assert adapter.reconcile(ref).ref.remote_task_id == "remote-7"
    assert first.session_id == "session-1" and first.worktree == "/work/remote-7"


def test_lost_create_response_reconciles_without_another_launch() -> None:
    class LostResponseApi(FakeApi):
        def create_and_launch(self, external_id, objective):
            self.create(external_id, objective)
            raise KandevTransportError("response lost")

    api = LostResponseApi()
    snapshot = _adapter(api=api).create_task(
        KandevTaskRef(7, 12, "molicoder-run-7-task-12"), "Implement parser",
    )
    assert snapshot.ref.remote_task_id == "remote-7"
    assert api.created == 1


def test_lost_create_response_without_remote_identity_stays_unresolved() -> None:
    class LostResponseApi(FakeApi):
        def create_and_launch(self, external_id, objective):
            raise KandevTransportError("response lost")

    with pytest.raises(KandevTransportError, match="response lost"):
        _adapter(api=LostResponseApi()).create_task(
            KandevTaskRef(7, 12, "molicoder-run-7-task-12"), "Implement parser",
        )


def test_unsettled_unknown_and_missing_remote_states_require_reconciliation() -> None:
    api = FakeApi()
    adapter = _adapter(api=api)
    ref = KandevTaskRef(7, 12, "molicoder-run-7-task-12")
    assert adapter.get_task(ref).state == "reconciliation_required"
    api.create(ref.external_id, "work")
    api.tasks[ref.external_id]["creation_complete"] = False
    assert adapter.get_task(ref).state == "reconciliation_required"
    api.tasks[ref.external_id]["creation_complete"] = True
    api.tasks[ref.external_id]["state"] = "SOMETHING_NEW"
    assert adapter.get_task(ref).state == "reconciliation_required"
    api.tasks[ref.external_id]["state"] = "COMPLETED"
    assert adapter.get_task(ref).state == "verifying"


def test_current_task_dto_fields_and_states_map_without_guessing_completion() -> None:
    api = FakeApi()
    adapter = _adapter(api=api)
    ref = KandevTaskRef(8, 13, "molicoder-run-8-task-13")
    raw = api.create(ref.external_id, "work")
    raw.pop("session_id")
    raw["primary_session_id"] = "session-current"
    raw["state"] = "TODO"
    assert adapter.get_task(ref).state == "ready"
    raw["state"] = "WAITING_FOR_INPUT"
    snapshot = adapter.get_task(ref)
    assert snapshot.state == "review_required"
    assert snapshot.session_id == "session-current"
    raw["state"] = "COMPLETED"
    assert adapter.get_task(ref).state == "verifying"


def test_cancel_request_does_not_claim_stop_and_events_keep_local_ids(tmp_path) -> None:
    store = ChatStore(tmp_path / "state.sqlite3")
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = store.upsert_project("Project", str(project_root), "python")
    run_id = store.create_run(project.id, "Build parser", "HYBRID")
    task_id = store.create_run_task(run_id, "T1", "Parser", "Implement parser", "low")
    api = FakeApi()
    adapter = _adapter(store=store, api=api)
    ref = KandevTaskRef(run_id, task_id, f"molicoder-run-{run_id}-task-{task_id}")
    created = adapter.create_task(ref, "Implement parser")
    events = adapter.subscribe_events(created.ref)
    assert len(events) == 1 and events[0].ref.task_id == task_id
    assert adapter.subscribe_events(created.ref, after_cursor="cursor-1") == ()
    assert adapter.cancel_task(created.ref).state == "running"
    assert api.cancel_requested
    api.tasks[ref.external_id]["state"] = "CANCELLED"
    assert adapter.reconcile(created.ref).state == "cancelled"
    assert any(event.kind == "kandev_task_updated" and event.task_id == task_id
               for event in store.list_events(run_id))
