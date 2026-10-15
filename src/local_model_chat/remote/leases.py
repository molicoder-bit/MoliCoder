"""Durable coordinator-owned worker and lease records with epoch fencing."""

from __future__ import annotations

import re
import base64
import binascii
import hashlib
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..storage import ChatStore


@dataclass(frozen=True, slots=True)
class RemoteLease:
    task_id: int
    worker_id: str
    epoch: int
    state: str
    base_commit: str
    worktree_root: str
    expires_at: str
    last_sequence: int


class LeaseConflict(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("remote lease time needs a timezone")
    return value.astimezone(timezone.utc).isoformat()


class RemoteLeaseStore:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    def pair_worker(self, worker_id: str, public_key: str, coordinator_fingerprint: str) -> None:
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", worker_id)
                or not re.fullmatch(r"[0-9a-f]{64}", coordinator_fingerprint)):
            raise ValueError("worker identity or certificate fingerprint is invalid")
        try:
            decoded_key = base64.b64decode(public_key, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("worker public key is invalid") from exc
        if len(decoded_key) != 32:
            raise ValueError("worker public key must be 32 bytes")
        key_digest = hashlib.sha256(decoded_key).hexdigest()
        scope = f"remote_pair:{worker_id}:{key_digest}:{coordinator_fingerprint}"
        AuthorizationPolicy(self.store).require(ActionRequest(
            None, "remote_pair", scope, ActionRisk.MUTATION,
        ))
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM remote_workers WHERE id = ?", (worker_id,)).fetchone():
                raise LeaseConflict("worker identity already exists; revoke before replacing")
            connection.execute(
                "INSERT INTO remote_workers(id, public_key, coordinator_fingerprint, status, created_at) "
                "VALUES (?, ?, ?, 'paired', ?)",
                (worker_id, public_key, coordinator_fingerprint, _timestamp(_now())),
            )
            connection.commit()

    def revoke_worker(self, worker_id: str) -> None:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM remote_workers WHERE id = ?", (worker_id,)).fetchone():
                raise KeyError(worker_id)
            connection.execute("UPDATE remote_workers SET status = 'revoked' WHERE id = ?", (worker_id,))
            connection.execute(
                "UPDATE remote_leases SET state = 'reconciling', updated_at = ? "
                "WHERE worker_id = ? AND state IN ('offered', 'active')",
                (_timestamp(_now()), worker_id),
            )
            connection.commit()

    def get(self, task_id: int) -> RemoteLease | None:
        with closing(self.store._connect()) as connection:
            row = connection.execute("SELECT * FROM remote_leases WHERE task_id = ?", (task_id,)).fetchone()
        return RemoteLease(**{key: row[key] for key in RemoteLease.__dataclass_fields__}) if row else None

    def offer(self, task_id: int, worker_id: str, *, base_commit: str,
              worktree_root: str, expires_at: datetime, now: datetime | None = None) -> RemoteLease:
        current_time = now or _now()
        if (not re.fullmatch(r"[0-9a-f]{40,64}", base_commit)
                or not worktree_root.strip() or expires_at.tzinfo is None
                or expires_at <= current_time):
            raise ValueError("lease needs a base commit, worktree root, and future expiry")
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            worker = connection.execute(
                "SELECT status FROM remote_workers WHERE id = ?", (worker_id,),
            ).fetchone()
            if not worker or worker["status"] != "paired":
                raise LeaseConflict("worker is not paired")
            task = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not task or task["status"] not in {"proposed", "ready", "retrying"}:
                raise LeaseConflict("task is not available for a remote lease")
            busy = connection.execute(
                "SELECT task_id FROM remote_leases WHERE worker_id = ? "
                "AND task_id != ? AND state IN ('offered', 'active', 'reconciling') LIMIT 1",
                (worker_id, task_id),
            ).fetchone()
            if busy:
                raise LeaseConflict("worker already owns an unresolved lease")
            previous = connection.execute(
                "SELECT epoch, state FROM remote_leases WHERE task_id = ?", (task_id,),
            ).fetchone()
            if previous and previous["state"] not in {"completed", "cancelled"}:
                raise LeaseConflict("task already has an unresolved lease")
            epoch = previous["epoch"] + 1 if previous else 1
            connection.execute(
                """INSERT INTO remote_leases(task_id, worker_id, epoch, state, base_commit,
                   worktree_root, expires_at, last_sequence, updated_at)
                   VALUES (?, ?, ?, 'offered', ?, ?, ?, 0, ?)
                   ON CONFLICT(task_id) DO UPDATE SET worker_id=excluded.worker_id,
                   epoch=excluded.epoch, state='offered', base_commit=excluded.base_commit,
                   worktree_root=excluded.worktree_root, expires_at=excluded.expires_at,
                   last_sequence=0, updated_at=excluded.updated_at""",
                (task_id, worker_id, epoch, base_commit, worktree_root,
                 _timestamp(expires_at), _timestamp(current_time)),
            )
            connection.commit()
        return self.get(task_id)

    def advance(self, task_id: int, worker_id: str, epoch: int, sequence: int,
                state: str, *, now: datetime | None = None) -> RemoteLease:
        if state not in {"active", "reconciling", "completed", "cancelled"} or sequence <= 0:
            raise ValueError("invalid lease transition")
        current_time = now or _now()
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT l.*, w.status AS worker_status FROM remote_leases l "
                "JOIN remote_workers w ON w.id = l.worker_id WHERE l.task_id = ?", (task_id,),
            ).fetchone()
            if (not row or row["worker_id"] != worker_id or row["epoch"] != epoch
                    or row["worker_status"] != "paired"):
                raise LeaseConflict("unknown, revoked, or stale lease")
            if sequence <= row["last_sequence"]:
                raise LeaseConflict("replayed or reordered lease message")
            if datetime.fromisoformat(row["expires_at"]) <= current_time:
                connection.execute(
                    "UPDATE remote_leases SET state = 'reconciling', updated_at = ? WHERE task_id = ?",
                    (_timestamp(current_time), task_id),
                )
                connection.commit()
                raise LeaseConflict("lease expired; reconcile before further work")
            allowed = {
                "offered": {"active", "cancelled"},
                "active": {"active", "reconciling", "completed", "cancelled"},
                "reconciling": {"active", "cancelled"},
            }
            if state not in allowed.get(row["state"], set()):
                raise LeaseConflict("invalid lease state transition")
            connection.execute(
                "UPDATE remote_leases SET state = ?, last_sequence = ?, updated_at = ? WHERE task_id = ?",
                (state, sequence, _timestamp(current_time), task_id),
            )
            connection.commit()
        return self.get(task_id)
