from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..storage import ChatStore


_RISKS = {
    "deployment": ActionRisk.EXTERNAL,
    "payment": ActionRisk.FINANCIAL,
    "destructive": ActionRisk.DESTRUCTIVE,
}


class ActionReviewRequired(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    idempotency_key: str
    kind: str
    scope: str
    payload_digest: str
    status: str
    external_receipt: str


class ActionReceipts:
    """Reserve before dispatch; unresolved dispatches block replay after a crash."""

    def __init__(self, store: ChatStore, project_id: int) -> None:
        project = store.get_project(project_id)
        if project is None:
            raise ValueError("project does not exist")
        root = Path(project.path).resolve()
        path = root / ".molicoder" / "action-receipts.sqlite3"
        if not path.resolve().is_relative_to(root):
            raise PermissionError("receipt database escapes the project")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.store = store
        self.project_id = project_id
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS action_receipts (
                       idempotency_key TEXT PRIMARY KEY,
                       kind TEXT NOT NULL, scope TEXT NOT NULL, payload_digest TEXT NOT NULL,
                       status TEXT NOT NULL CHECK(status IN ('in_progress','completed','uncertain')),
                       external_receipt TEXT NOT NULL DEFAULT '',
                       created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                   )"""
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def scope_for(project_id: int, kind: str, key: str, payload: bytes) -> str:
        if kind not in _RISKS:
            raise ActionReviewRequired("unsupported external action requires user review")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,100}", key):
            raise ValueError("external action needs a stable idempotency key")
        digest = hashlib.sha256(payload).hexdigest()
        return f"project:{project_id}:kind:{kind}:idempotency:{key}:sha256:{digest}"

    def get(self, key: str) -> ActionReceipt | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM action_receipts WHERE idempotency_key = ?", (key,),
            ).fetchone()
        if row is None:
            return None
        return ActionReceipt(
            row["idempotency_key"], row["kind"], row["scope"],
            row["payload_digest"], row["status"], row["external_receipt"],
        )

    def execute_once(
        self, kind: str, key: str, payload: bytes,
        dispatch: Callable[[str], str],
    ) -> ActionReceipt:
        scope = self.scope_for(self.project_id, kind, key, payload)
        AuthorizationPolicy(self.store).require(ActionRequest(
            self.project_id, "external_action", scope, _RISKS[kind],
        ))
        digest = hashlib.sha256(payload).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_receipts WHERE idempotency_key = ?", (key,),
            ).fetchone()
            if row is not None:
                if row["kind"] != kind or row["scope"] != scope or row["payload_digest"] != digest:
                    raise ActionReviewRequired("idempotency key was already used for a different action")
                if row["status"] == "completed":
                    return ActionReceipt(key, kind, scope, digest, "completed", row["external_receipt"])
                raise ActionReviewRequired("external action outcome is uncertain; reconcile before retry")
            connection.execute(
                """INSERT INTO action_receipts(
                       idempotency_key, kind, scope, payload_digest, status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'in_progress', ?, ?)""",
                (key, kind, scope, digest, now, now),
            )
            connection.commit()
        try:
            external_receipt = dispatch(key)
            if (not isinstance(external_receipt, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", external_receipt)):
                raise ValueError("external action returned no verifiable receipt")
        except Exception as exc:
            with closing(self._connect()) as connection:
                connection.execute(
                    "UPDATE action_receipts SET status = 'uncertain', updated_at = ? WHERE idempotency_key = ?",
                    (datetime.now(timezone.utc).isoformat(), key),
                )
                connection.commit()
            raise ActionReviewRequired("external action outcome is uncertain; reconcile before retry") from exc
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE action_receipts SET status = 'completed', external_receipt = ?, updated_at = ?
                   WHERE idempotency_key = ? AND status = 'in_progress'""",
                (external_receipt, datetime.now(timezone.utc).isoformat(), key),
            )
            connection.commit()
        return self.get(key)

    def reconcile_completed(
        self, kind: str, key: str, payload: bytes, external_receipt: str,
    ) -> ActionReceipt:
        """Record a human-verified external outcome without dispatching again."""
        scope = self.scope_for(self.project_id, kind, key, payload)
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", external_receipt):
            raise ValueError("reconciliation requires a verifiable external receipt")
        AuthorizationPolicy(self.store).require(ActionRequest(
            self.project_id, "external_action_reconcile", scope, _RISKS[kind],
        ))
        digest = hashlib.sha256(payload).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_receipts WHERE idempotency_key = ?", (key,),
            ).fetchone()
            if row is None:
                raise ActionReviewRequired("external action has no reserved receipt to reconcile")
            if row["kind"] != kind or row["scope"] != scope or row["payload_digest"] != digest:
                raise ActionReviewRequired("external action identity differs from the reserved receipt")
            if row["status"] == "completed":
                if row["external_receipt"] != external_receipt:
                    raise ActionReviewRequired("external action already has a different verified receipt")
            else:
                connection.execute(
                    """UPDATE action_receipts SET status = 'completed', external_receipt = ?, updated_at = ?
                       WHERE idempotency_key = ? AND status IN ('in_progress', 'uncertain')""",
                    (external_receipt, datetime.now(timezone.utc).isoformat(), key),
                )
                connection.commit()
        receipt = self.get(key)
        assert receipt is not None
        return receipt
