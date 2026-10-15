from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import base64
import hashlib

import pytest

from local_model_chat.remote.leases import LeaseConflict, RemoteLeaseStore
from local_model_chat.storage import ChatStore


FINGERPRINT = "a" * 64
BASE = "b" * 40


def _setup(tmp_path):
    store = ChatStore(tmp_path / "remote.sqlite3")
    project = store.upsert_project("Remote fixture", str(tmp_path), "python")
    run_id = store.create_run(project.id, "Harmless remote test", "LOCAL_OFFLINE")
    task_id = store.create_run_task(run_id, "T1", "Read file", "Report a line", "low")
    leases = RemoteLeaseStore(store)
    for worker in ("worker-a", "worker-b"):
        key = bytes([1 if worker == "worker-a" else 2]) * 32
        public_key = base64.b64encode(key).decode("ascii")
        store.record_action_authorization(
            project_id=None, action="remote_pair",
            scope=f"remote_pair:{worker}:{hashlib.sha256(key).hexdigest()}:{FINGERPRINT}", risk="mutation",
            decision="approved", actor="user",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        leases.pair_worker(worker, public_key, FINGERPRINT)
    return store, leases, task_id


def test_remote_lease_fences_stale_workers_and_replayed_messages(tmp_path) -> None:
    _store, leases, task_id = _setup(tmp_path)
    now = datetime.now(timezone.utc)
    first = leases.offer(task_id, "worker-a", base_commit=BASE,
                         worktree_root="remote/checkout-a", expires_at=now + timedelta(minutes=5), now=now)
    with pytest.raises(LeaseConflict, match="unresolved"):
        leases.offer(task_id, "worker-b", base_commit=BASE,
                     worktree_root="remote/checkout-b", expires_at=now + timedelta(minutes=5), now=now)
    leases.advance(task_id, "worker-a", first.epoch, 1, "active", now=now)
    with pytest.raises(LeaseConflict, match="replayed"):
        leases.advance(task_id, "worker-a", first.epoch, 1, "active", now=now)
    leases.advance(task_id, "worker-a", first.epoch, 2, "cancelled", now=now)
    second = leases.offer(task_id, "worker-b", base_commit=BASE,
                          worktree_root="remote/checkout-b", expires_at=now + timedelta(minutes=5), now=now)
    assert second.epoch == 2 and second.worker_id == "worker-b"
    with pytest.raises(LeaseConflict, match="stale"):
        leases.advance(task_id, "worker-a", first.epoch, 3, "completed", now=now)
    assert RemoteLeaseStore(_store).get(task_id) == second


def test_remote_expiry_and_revocation_hold_lease_for_reconciliation(tmp_path) -> None:
    _store, leases, task_id = _setup(tmp_path)
    now = datetime.now(timezone.utc)
    first = leases.offer(task_id, "worker-a", base_commit=BASE,
                         worktree_root="remote/checkout-a", expires_at=now + timedelta(seconds=1), now=now)
    with pytest.raises(LeaseConflict, match="expired"):
        leases.advance(task_id, "worker-a", first.epoch, 1, "active", now=now + timedelta(seconds=2))
    assert leases.get(task_id).state == "reconciling"
    with pytest.raises(LeaseConflict, match="unresolved"):
        leases.offer(task_id, "worker-b", base_commit=BASE,
                     worktree_root="remote/checkout-b", expires_at=now + timedelta(minutes=5), now=now)
    leases.revoke_worker("worker-a")
    with pytest.raises(LeaseConflict, match="revoked"):
        leases.advance(task_id, "worker-a", first.epoch, 2, "active", now=now)


def test_remote_offer_is_atomic_across_coordinator_connections(tmp_path) -> None:
    store, leases, task_id = _setup(tmp_path)
    now = datetime.now(timezone.utc)

    def offer(worker):
        try:
            RemoteLeaseStore(store).offer(task_id, worker, base_commit=BASE,
                                          worktree_root=f"remote/{worker}",
                                          expires_at=now + timedelta(minutes=5), now=now)
            return "offered"
        except LeaseConflict:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(offer, ("worker-a", "worker-b")))
    assert sorted(outcomes) == ["offered", "rejected"]
    assert leases.get(task_id).epoch == 1
