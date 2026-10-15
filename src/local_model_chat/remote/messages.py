"""Signed, bounded worker control messages with durable idempotent receipts.

This is a coordinator-side parser. No socket is bound and no task is executed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..storage import ChatStore
from .leases import LeaseConflict


MAX_ENVELOPE_BYTES = 64 * 1024
MAX_CLOCK_SKEW = timedelta(minutes=5)
CONTROL_KINDS = frozenset({
    "lease_accept", "heartbeat", "reconcile", "request_inference", "artifact_manifest", "cancel_ack",
})


@dataclass(frozen=True, slots=True)
class MessageReceipt:
    message_id: str
    task_id: int
    epoch: int
    sequence: int
    received_at: str


def canonical_bytes(envelope: dict) -> bytes:
    if not isinstance(envelope, dict):
        raise ValueError("worker envelope must be an object")
    try:
        encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("worker envelope is not valid JSON") from exc
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise ValueError("worker envelope exceeds size limit")
    return encoded


class RemoteMessageStore:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    def receive(self, envelope: dict, signature_b64: str, *,
                now: datetime | None = None) -> MessageReceipt:
        encoded = canonical_bytes(envelope)
        required = {
            "protocol", "message_id", "worker_id", "task_id", "epoch", "sequence",
            "nonce", "sent_at", "coordinator_fingerprint", "kind", "body",
        }
        if set(envelope) != required or envelope["protocol"] != 1:
            raise ValueError("worker envelope fields or protocol are unsupported")
        if (not isinstance(envelope["task_id"], int) or isinstance(envelope["task_id"], bool)
                or envelope["task_id"] <= 0
                or not isinstance(envelope["epoch"], int) or isinstance(envelope["epoch"], bool)
                or envelope["epoch"] <= 0
                or not isinstance(envelope["sequence"], int) or isinstance(envelope["sequence"], bool)
                or envelope["sequence"] <= 0
                or not isinstance(envelope["body"], dict)
                or not isinstance(envelope["worker_id"], str)
                or not isinstance(envelope["kind"], str)
                or envelope["kind"] not in CONTROL_KINDS
                or not isinstance(envelope["coordinator_fingerprint"], str)
                or not isinstance(envelope["message_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", envelope["message_id"])
                or not isinstance(envelope["nonce"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", envelope["nonce"])):
            raise ValueError("worker envelope identity or control kind is invalid")
        if not isinstance(signature_b64, str):
            raise ValueError("worker signature is invalid")
        try:
            sent = datetime.fromisoformat(envelope["sent_at"])
        except (TypeError, ValueError) as exc:
            raise ValueError("worker message timestamp is invalid") from exc
        current = now or datetime.now(timezone.utc)
        if sent.tzinfo is None or current.tzinfo is None or abs(sent - current) > MAX_CLOCK_SKEW:
            raise ValueError("worker message timestamp is stale")
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("worker signature is invalid") from exc
        if len(signature) != 64:
            raise ValueError("worker signature is invalid")
        digest = hashlib.sha256(encoded).hexdigest()
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            worker = connection.execute(
                "SELECT public_key, coordinator_fingerprint, status FROM remote_workers WHERE id = ?",
                (envelope["worker_id"],),
            ).fetchone()
            if (worker is None or worker["status"] != "paired"
                    or worker["coordinator_fingerprint"] != envelope["coordinator_fingerprint"]):
                raise LeaseConflict("worker identity, coordinator pin, or pairing is invalid")
            public_key = base64.b64decode(worker["public_key"], validate=True)
            try:
                Ed25519PublicKey.from_public_bytes(public_key).verify(signature, encoded)
            except (InvalidSignature, ValueError) as exc:
                raise LeaseConflict("worker message signature is invalid") from exc
            previous = connection.execute(
                "SELECT * FROM remote_message_receipts WHERE message_id = ?",
                (envelope["message_id"],),
            ).fetchone()
            if previous:
                if previous["envelope_sha256"] != digest:
                    raise LeaseConflict("message ID reused for a different envelope")
                return MessageReceipt(previous["message_id"], previous["task_id"],
                                      previous["epoch"], previous["sequence"], previous["received_at"])
            if connection.execute(
                "SELECT 1 FROM remote_message_receipts WHERE worker_id = ? AND nonce = ?",
                (envelope["worker_id"], envelope["nonce"]),
            ).fetchone():
                raise LeaseConflict("worker nonce was reused")
            lease = connection.execute(
                "SELECT * FROM remote_leases WHERE task_id = ?", (envelope["task_id"],),
            ).fetchone()
            if (not lease or lease["worker_id"] != envelope["worker_id"]
                    or lease["epoch"] != envelope["epoch"]):
                raise LeaseConflict("worker message belongs to an unknown or stale lease")
            if envelope["sequence"] <= lease["last_sequence"]:
                raise LeaseConflict("worker sequence was replayed or reordered")
            expired = datetime.fromisoformat(lease["expires_at"]) <= current
            kind = envelope["kind"]
            cancel_intent = connection.execute(
                "SELECT acknowledged_at FROM remote_cancel_intents WHERE task_id = ? AND epoch = ?",
                (envelope["task_id"], envelope["epoch"]),
            ).fetchone()
            cancel_pending = cancel_intent is not None and cancel_intent["acknowledged_at"] is None
            if cancel_pending and kind in {"lease_accept", "request_inference", "artifact_manifest"}:
                raise LeaseConflict("remote cancellation blocks new dispatches")
            if expired and kind not in {"reconcile", "cancel_ack"} and lease["state"] in {"offered", "active"}:
                connection.execute(
                    "UPDATE remote_leases SET state = 'reconciling', updated_at = ? WHERE task_id = ?",
                    (current.astimezone(timezone.utc).isoformat(), envelope["task_id"]),
                )
                connection.commit()
                raise LeaseConflict("lease expired; reconciliation required")
            if kind == "lease_accept" and (expired or lease["state"] != "offered"):
                raise LeaseConflict("lease cannot be accepted")
            if kind == "heartbeat" and (expired or lease["state"] != "active"):
                raise LeaseConflict("heartbeat requires an active unexpired lease")
            if kind == "request_inference" and (expired or lease["state"] != "active"):
                raise LeaseConflict("inference requires an active unexpired lease")
            if kind == "artifact_manifest" and (expired or lease["state"] != "active"):
                raise LeaseConflict("artifact report requires an active unexpired lease")
            if kind == "reconcile" and lease["state"] not in {"active", "reconciling"}:
                raise LeaseConflict("lease is not ready for reconciliation")
            if kind == "cancel_ack":
                if (not cancel_pending or lease["state"] not in {"offered", "active", "reconciling"}
                        or envelope["body"] != {"stopped": True}):
                    raise LeaseConflict("cancellation acknowledgement is invalid")
                pending = connection.execute(
                    """SELECT 1 FROM inference_requests r WHERE r.task_id = ? AND NOT EXISTS (
                       SELECT 1 FROM inference_usage_events e WHERE e.request_id = r.request_id
                       AND e.status IN ('completed', 'failed', 'cancelled', 'denied')) LIMIT 1""",
                    (envelope["task_id"],),
                ).fetchone()
                if pending:
                    raise LeaseConflict("unsettled inference prevents cancellation acknowledgement")
            state = (
                "active" if kind == "lease_accept" else
                "reconciling" if kind == "reconcile" else
                "cancelled" if kind == "cancel_ack" else lease["state"]
            )
            received_at = current.astimezone(timezone.utc).isoformat()
            connection.execute(
                "UPDATE remote_leases SET state = ?, last_sequence = ?, updated_at = ? WHERE task_id = ?",
                (state, envelope["sequence"], received_at, envelope["task_id"]),
            )
            if kind == "cancel_ack":
                connection.execute(
                    "UPDATE remote_cancel_intents SET acknowledged_at = ? WHERE task_id = ? AND epoch = ?",
                    (received_at, envelope["task_id"], envelope["epoch"]),
                )
            connection.execute(
                """INSERT INTO remote_message_receipts(
                   message_id, task_id, worker_id, epoch, sequence, nonce, envelope_sha256, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (envelope["message_id"], envelope["task_id"], envelope["worker_id"],
                 envelope["epoch"], envelope["sequence"], envelope["nonce"], digest, received_at),
            )
            connection.commit()
        return MessageReceipt(envelope["message_id"], envelope["task_id"],
                              envelope["epoch"], envelope["sequence"], received_at)
