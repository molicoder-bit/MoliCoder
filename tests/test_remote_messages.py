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
    run_id = store.create_run(project.id, "Read one file", "LOCAL_OFFLINE")
    task_id = store.create_run_task(run_id, "T1", "Read", "Read one file", "low")
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
    assert len(list(tmp_path.glob("messages.sqlite3.pre-v13-*.bak"))) == 1
    envelope = _envelope(lease, now)
    assert RemoteMessageStore(upgraded).receive(
        envelope, _signature(private, envelope), now=now,
    ).message_id == envelope["message_id"]
