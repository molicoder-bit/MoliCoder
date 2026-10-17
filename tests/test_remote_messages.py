import base64
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_model_chat.remote.leases import LeaseConflict, RemoteLeaseStore
from local_model_chat.remote.messages import RemoteMessageStore, canonical_bytes
from local_model_chat.storage import ChatStore


def _fixture(tmp_path):
    store = ChatStore(tmp_path / "messages.sqlite3")
    project = store.upsert_project("Signed messages", str(tmp_path), "python")
    run_id = store.create_run(project.id, "Read one file", "REMOTE_OPT_IN",
                              provider="remote", base_commit="b" * 40)
    task_id = store.create_run_task(run_id, "T1", "Read", "Read one file", "low")
    store.transition_task_state(task_id, "ready", attempts=0,
                                reason="remote fixture ready", actor="system")
    private = Ed25519PrivateKey.generate()
    key = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    fingerprint = "f" * 64
    store.record_action_authorization(
        project_id=None, action="remote_pair",
        scope=f"remote_pair:worker-a:{hashlib.sha256(key).hexdigest()}:{fingerprint}",
        risk="mutation", decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    leases = RemoteLeaseStore(store)
    leases.pair_worker("worker-a", base64.b64encode(key).decode(), fingerprint)
    now = datetime.now(timezone.utc)
    lease = leases.offer(task_id, "worker-a", base_commit="b" * 40,
                         worktree_root="remote/fixture", expires_at=now + timedelta(minutes=2), now=now)
    return store, leases, private, lease, now


def _envelope(lease, now, *, sequence=1, kind="lease_accept", nonce="a" * 32, message_id="message-00000001"):
    return {
        "protocol": 1, "message_id": message_id, "worker_id": "worker-a",
        "task_id": lease.task_id, "epoch": lease.epoch, "sequence": sequence,
        "nonce": nonce, "sent_at": now.isoformat(), "coordinator_fingerprint": "f" * 64,
        "kind": kind, "body": {},
    }


def _signature(private, envelope):
    return base64.b64encode(private.sign(canonical_bytes(envelope))).decode()


def test_signed_messages_have_durable_idempotent_receipts_and_sequence_fencing(tmp_path) -> None:
    store, leases, private, lease, now = _fixture(tmp_path)
    messages = RemoteMessageStore(store)
    first = _envelope(lease, now)
    signature = _signature(private, first)
    receipt = messages.receive(first, signature, now=now)
    assert receipt == RemoteMessageStore(store).receive(first, signature, now=now)
    assert leases.get(lease.task_id).state == "active"
    changed = {**first, "body": {"different": True}}
    with pytest.raises(LeaseConflict, match="message ID reused"):
        messages.receive(changed, _signature(private, changed), now=now)
    second = _envelope(lease, now, sequence=2, kind="heartbeat",
                       nonce="b" * 32, message_id="message-00000002")
    messages.receive(second, _signature(private, second), now=now)
    with pytest.raises(LeaseConflict, match="replayed"):
        repeated = _envelope(lease, now, sequence=2, kind="heartbeat",
                             nonce="c" * 32, message_id="message-00000003")
        messages.receive(repeated, _signature(private, repeated), now=now)
    with pytest.raises(LeaseConflict, match="nonce"):
        reused_nonce = _envelope(lease, now, sequence=3, kind="heartbeat",
                                 nonce="b" * 32, message_id="message-00000004")
        messages.receive(reused_nonce, _signature(private, reused_nonce), now=now)
    leases.revoke_worker("worker-a")
    with pytest.raises(LeaseConflict, match="pairing"):
        messages.receive(first, signature, now=now)


def test_signed_messages_reject_wrong_key_pin_epoch_and_oversize(tmp_path) -> None:
    store, leases, private, lease, now = _fixture(tmp_path)
    messages = RemoteMessageStore(store)
    envelope = _envelope(lease, now)
    wrong_key = Ed25519PrivateKey.generate()
    with pytest.raises(LeaseConflict, match="signature"):
        messages.receive(envelope, _signature(wrong_key, envelope), now=now)
    wrong_pin = {**envelope, "coordinator_fingerprint": "e" * 64}
    with pytest.raises(LeaseConflict, match="pin"):
        messages.receive(wrong_pin, _signature(private, wrong_pin), now=now)
    wrong_epoch = {**envelope, "epoch": lease.epoch + 1}
    with pytest.raises(LeaseConflict, match="stale"):
        messages.receive(wrong_epoch, _signature(private, wrong_epoch), now=now)
    huge = {**envelope, "body": {"text": "x" * 70_000}}
    with pytest.raises(ValueError, match="size limit"):
        canonical_bytes(huge)
    assert leases.get(lease.task_id).last_sequence == 0


def test_expired_lease_reconciles_without_accepting_new_heartbeat(tmp_path) -> None:
    store, leases, private, lease, now = _fixture(tmp_path)
    first = _envelope(lease, now)
    messages = RemoteMessageStore(store)
    messages.receive(first, _signature(private, first), now=now)
    later = now + timedelta(minutes=3)
    heartbeat = _envelope(lease, later, sequence=2, kind="heartbeat",
                          nonce="b" * 32, message_id="message-00000002")
    with pytest.raises(LeaseConflict, match="expired"):
        messages.receive(heartbeat, _signature(private, heartbeat), now=later)
    assert leases.get(lease.task_id).state == "reconciling"
    reconcile = _envelope(lease, later, sequence=2, kind="reconcile",
                          nonce="c" * 32, message_id="message-00000003")
    messages.receive(reconcile, _signature(private, reconcile), now=later)
    assert leases.get(lease.task_id).state == "reconciling"


def test_v12_upgrade_preserves_lease_and_adds_message_receipts(tmp_path) -> None:
    store, leases, private, lease, now = _fixture(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE remote_message_receipts")
        connection.execute("PRAGMA user_version = 12")
    upgraded = ChatStore(store.path)
    assert RemoteLeaseStore(upgraded).get(lease.task_id) == lease
    assert len(list(tmp_path.glob("messages.sqlite3.pre-v16-*.bak"))) == 1
    envelope = _envelope(lease, now)
    assert RemoteMessageStore(upgraded).receive(
        envelope, _signature(private, envelope), now=now,
    ).message_id == envelope["message_id"]


def test_reconnect_needs_signed_reconcile_and_exact_renewal_approval(tmp_path) -> None:
    store, leases, private, lease, now = _fixture(tmp_path)
    messages = RemoteMessageStore(store)
    accept = _envelope(lease, now)
    messages.receive(accept, _signature(private, accept), now=now)
    later = now + timedelta(minutes=3)
    reconcile = _envelope(lease, later, sequence=2, kind="reconcile",
                          nonce="b" * 32, message_id="message-00000002")
    signature = _signature(private, reconcile)
    messages.receive(reconcile, signature, now=later)
    renewal = later + timedelta(minutes=10)
    with pytest.raises(PermissionError, match="approval"):
        leases.renew_after_reconcile(reconcile, signature, renewal, now=later)
    scope = (f"remote_renew:task:{lease.task_id}:epoch:{lease.epoch}:"
             f"sequence:2:expires:{renewal.isoformat()}")
    store.record_action_authorization(
        project_id=None, action="remote_renew", scope=scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(later + timedelta(minutes=15)).isoformat(),
    )
    from local_model_chat.inference.adapters.ollama import ensure_local_offering, local_offering_id
    from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
    from local_model_chat.inference.ledger import InferenceLedger, UsageEvent
    from local_model_chat.inference.registry import ProviderRegistry
    with store._connect() as connection:
        row = connection.execute(
            "SELECT t.run_id, r.project_id FROM tasks t JOIN runs r ON r.id = t.run_id "
            "WHERE t.id = ?", (lease.task_id,),
        ).fetchone()
    ensure_local_offering(ProviderRegistry(store), "molicoder:latest",
                          context_window=4096, max_output_tokens=256)
    pending = InferenceRequest(
        "remote-pending", row["project_id"], local_offering_id("molicoder:latest"),
        "molicoder:latest", InferencePurpose.EXECUTION,
        ({"role": "user", "content": "Hi"},), "private", (), 100, 20, 5,
        run_id=row["run_id"], task_id=lease.task_id,
    )
    InferenceLedger(store).start(pending)
    with pytest.raises(LeaseConflict, match="unsettled inference"):
        leases.renew_after_reconcile(reconcile, signature, renewal, now=later)
    InferenceLedger(store).append(UsageEvent(pending.request_id, "failed"))
    renewed = leases.renew_after_reconcile(reconcile, signature, renewal, now=later)
    assert renewed.state == "active" and renewed.expires_at == renewal.isoformat()
    with pytest.raises(LeaseConflict, match="reconciled lease changed"):
        leases.renew_after_reconcile(reconcile, signature, renewal, now=later)


def test_cancel_intent_blocks_dispatch_until_signed_worker_ack(tmp_path) -> None:
    store, leases, private, lease, now = _fixture(tmp_path)
    messages = RemoteMessageStore(store)
    accept = _envelope(lease, now)
    messages.receive(accept, _signature(private, accept), now=now)
    assert leases.request_cancel(lease.task_id, reason="User stopped this remote task")
    assert not leases.request_cancel(lease.task_id, reason="User stopped this remote task")
    assert leases.cancel_requested(lease.task_id, lease.epoch)
    request = _envelope(lease, now, sequence=2, kind="request_inference",
                        nonce="b" * 32, message_id="message-00000002")
    with pytest.raises(LeaseConflict, match="cancellation blocks"):
        messages.receive(request, _signature(private, request), now=now)
    heartbeat = _envelope(lease, now, sequence=2, kind="heartbeat",
                          nonce="c" * 32, message_id="message-00000003")
    messages.receive(heartbeat, _signature(private, heartbeat), now=now)
    ack = _envelope(lease, now, sequence=3, kind="cancel_ack",
                    nonce="d" * 32, message_id="message-00000004")
    ack["body"] = {"stopped": True}
    signature = _signature(private, ack)
    receipt = messages.receive(ack, signature, now=now)
    assert messages.receive(ack, signature, now=now) == receipt
    assert leases.get(lease.task_id).state == "cancelled"
    assert not leases.cancel_requested(lease.task_id, lease.epoch)


def test_cancel_ack_waits_for_uncertain_paid_dispatch(tmp_path) -> None:
    from local_model_chat.inference.adapters.ollama import ensure_local_offering, local_offering_id
    from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
    from local_model_chat.inference.ledger import InferenceLedger, UsageEvent
    from local_model_chat.inference.registry import ProviderRegistry

    store, leases, private, lease, now = _fixture(tmp_path)
    messages = RemoteMessageStore(store)
    accept = _envelope(lease, now)
    messages.receive(accept, _signature(private, accept), now=now)
    leases.request_cancel(lease.task_id, reason="Stop after dispatch settles")
    with store._connect() as connection:
        row = connection.execute(
            "SELECT t.run_id, r.project_id FROM tasks t JOIN runs r ON r.id = t.run_id "
            "WHERE t.id = ?", (lease.task_id,),
        ).fetchone()
    ensure_local_offering(ProviderRegistry(store), "molicoder:latest",
                          context_window=4096, max_output_tokens=256)
    pending = InferenceRequest(
        "remote-cancel-pending", row["project_id"], local_offering_id("molicoder:latest"),
        "molicoder:latest", InferencePurpose.EXECUTION,
        ({"role": "user", "content": "Hi"},), "private", (), 100, 20, 5,
        run_id=row["run_id"], task_id=lease.task_id,
    )
    InferenceLedger(store).start(pending)
    ack = _envelope(lease, now, sequence=2, kind="cancel_ack",
                    nonce="e" * 32, message_id="message-00000005")
    ack["body"] = {"stopped": True}
    with pytest.raises(LeaseConflict, match="unsettled inference"):
        messages.receive(ack, _signature(private, ack), now=now)
    assert leases.get(lease.task_id).state == "active"
    InferenceLedger(store).append(UsageEvent(pending.request_id, "failed"))
    messages.receive(ack, _signature(private, ack), now=now)
    assert leases.get(lease.task_id).state == "cancelled"


def test_offered_worker_can_poll_and_ack_cancellation_without_accepting(tmp_path) -> None:
    store, leases, private, lease, now = _fixture(tmp_path)
    leases.request_cancel(lease.task_id, reason="Cancel before the worker starts")
    poll = _envelope(lease, now, kind="status_poll")
    RemoteMessageStore(store).receive(poll, _signature(private, poll), now=now)
    assert leases.get(lease.task_id).state == "offered"
    assert leases.cancel_requested(lease.task_id, lease.epoch)
    ack = _envelope(lease, now, sequence=2, kind="cancel_ack",
                    nonce="f" * 32, message_id="message-00000006")
    ack["body"] = {"stopped": True}
    RemoteMessageStore(store).receive(ack, _signature(private, ack), now=now)
    assert leases.get(lease.task_id).state == "cancelled"
