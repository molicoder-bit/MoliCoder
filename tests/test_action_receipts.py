import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.actions.receipts import ActionReceipts, ActionReviewRequired
from local_model_chat.storage import ChatStore


def _service(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = ChatStore(tmp_path / "receipts.sqlite3")
    project = store.upsert_project("Sample", str(root), "python")
    return store, project, ActionReceipts(store, project.id)


def _authorize(store, project, kind, key, payload):
    scope = ActionReceipts.scope_for(project.id, kind, key, payload)
    risk = {"deployment": "external", "payment": "financial", "destructive": "destructive"}[kind]
    store.record_action_authorization(
        project_id=project.id, action="external_action", scope=scope, risk=risk,
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )


@pytest.mark.parametrize("kind", ["deployment", "payment", "destructive"])
def test_receipt_prevents_duplicate_external_dispatch(tmp_path, kind):
    store, project, service = _service(tmp_path)
    key, payload = f"action-{kind}", b"fixed approved payload"
    dispatched = []
    with pytest.raises(PermissionError):
        service.execute_once(kind, key, payload, lambda _key: dispatched.append(1) or "provider-123")
    _authorize(store, project, kind, key, payload)
    first = service.execute_once(kind, key, payload, lambda _key: dispatched.append(1) or "provider-123")
    second = service.execute_once(kind, key, payload, lambda _key: dispatched.append(2) or "provider-456")
    assert first == second
    assert first.status == "completed" and first.external_receipt == "provider-123"
    assert dispatched == [1]


def test_uncertain_or_crashed_action_blocks_retry_for_review(tmp_path):
    store, project, service = _service(tmp_path)
    key, payload = "payment-123", b"charge request"
    _authorize(store, project, "payment", key, payload)
    calls = []

    def ambiguous_dispatch(_key):
        calls.append(1)
        raise TimeoutError("network dropped after the payment request")

    with pytest.raises(ActionReviewRequired, match="uncertain"):
        service.execute_once("payment", key, payload, ambiguous_dispatch)
    restarted = ActionReceipts(store, project.id)
    with pytest.raises(ActionReviewRequired, match="uncertain"):
        restarted.execute_once("payment", key, payload, lambda _key: calls.append(2) or "new-charge")
    assert calls == [1] and restarted.get(key).status == "uncertain"

    crash_key = "deploy-crash-123"
    crash_payload = b"deploy request"
    _authorize(store, project, "deployment", crash_key, crash_payload)
    scope = ActionReceipts.scope_for(project.id, "deployment", crash_key, crash_payload)
    with sqlite3.connect(service.path) as connection:
        connection.execute(
            """INSERT INTO action_receipts(
                   idempotency_key, kind, scope, payload_digest, status, created_at, updated_at
               ) VALUES (?, 'deployment', ?, ?, 'in_progress', ?, ?)""",
            (crash_key, scope, scope.rsplit(":", 1)[-1], "now", "now"),
        )
    with pytest.raises(ActionReviewRequired, match="uncertain"):
        restarted.execute_once("deployment", crash_key, crash_payload, lambda _key: calls.append(3) or "receipt")
    assert calls == [1]


def test_unsupported_external_action_requires_review_without_dispatch(tmp_path):
    _store, _project, service = _service(tmp_path)
    called = []
    with pytest.raises(ActionReviewRequired, match="unsupported"):
        service.execute_once("send_email", "email-123456", b"hello", lambda _key: called.append(1) or "sent")
    assert called == []


def test_invalid_provider_receipt_stays_uncertain(tmp_path):
    store, project, service = _service(tmp_path)
    key, payload = "deploy-invalid-123", b"deploy request"
    _authorize(store, project, "deployment", key, payload)
    with pytest.raises(ActionReviewRequired, match="uncertain"):
        service.execute_once("deployment", key, payload, lambda _key: "api_key=secret")
    assert service.get(key).status == "uncertain"
