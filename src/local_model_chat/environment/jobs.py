from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..storage import ChatStore
from .install import InstallReceipt
from .policy import SetupPlan


@dataclass(frozen=True, slots=True)
class InstallJob:
    id: int
    tool_id: str
    version: str
    scope: str
    manifest_sha256: str
    risk: str
    status: str
    receipt_json: str
    error: str
    created_at: str
    updated_at: str


class InstallJobQueue:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def enqueue(self, plan: SetupPlan) -> int:
        if plan.action != "install_managed" or not plan.sha256:
            raise ValueError("only managed install plans can be queued")
        now = self._now()
        status = "queued" if plan.authorized else "waiting_for_authorization"
        risk = ActionRisk.PRIVILEGED if plan.privilege == "administrator" else ActionRisk.MUTATION
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT id FROM installation_jobs WHERE scope = ? AND manifest_sha256 = ?
                   AND status IN ('queued', 'running', 'waiting_for_authorization',
                                  'waiting_for_login', 'waiting_for_permission', 'completed')
                   ORDER BY id DESC LIMIT 1""",
                (plan.scope, plan.sha256),
            ).fetchone()
            if existing:
                connection.commit()
                return int(existing[0])
            cursor = connection.execute(
                """INSERT INTO installation_jobs(
                    tool_id, version, scope, manifest_sha256, risk, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (plan.tool_id, plan.version, plan.scope, plan.sha256, risk.value, status, now, now),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def list_jobs(self) -> tuple[InstallJob, ...]:
        with closing(self.store._connect()) as connection:
            rows = connection.execute("SELECT * FROM installation_jobs ORDER BY id").fetchall()
        return tuple(InstallJob(**dict(row)) for row in rows)

    def claim_next(self) -> InstallJob | None:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM installation_jobs WHERE status = 'queued' ORDER BY id"
            ).fetchall()
            for row in rows:
                approval = AuthorizationPolicy(self.store).check(ActionRequest(
                    None, "setup", row["scope"], ActionRisk(row["risk"]),
                ))
                now = self._now()
                if not approval.allowed:
                    connection.execute(
                        """UPDATE installation_jobs SET status = 'waiting_for_authorization',
                           updated_at = ? WHERE id = ?""", (now, row["id"]),
                    )
                    continue
                connection.execute(
                    "UPDATE installation_jobs SET status = 'running', updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                connection.commit()
                return InstallJob(**{**dict(row), "status": "running", "updated_at": now})
            connection.commit()
            return None

    def refresh_waiting_authorizations(self) -> tuple[int, ...]:
        changed = []
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM installation_jobs WHERE status = 'waiting_for_authorization'"
            ).fetchall()
            for row in rows:
                if AuthorizationPolicy(self.store).check(ActionRequest(
                    None, "setup", row["scope"], ActionRisk(row["risk"]),
                )).allowed:
                    connection.execute(
                        "UPDATE installation_jobs SET status = 'queued', updated_at = ? WHERE id = ?",
                        (self._now(), row["id"]),
                    )
                    changed.append(int(row["id"]))
            connection.commit()
        return tuple(changed)

    def transition(
        self, job_id: int, status: str, *, receipt: InstallReceipt | None = None, error: str = "",
    ) -> None:
        if status not in {
            "waiting_for_login", "waiting_for_permission", "completed", "failed", "queued",
        }:
            raise ValueError("invalid install job transition")
        if status == "completed" and receipt is None:
            raise ValueError("completed setup requires a receipt")
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM installation_jobs WHERE id = ?", (job_id,),
            ).fetchone()
            if row is None or row[0] not in {"running", "waiting_for_login", "waiting_for_permission"}:
                raise ValueError("job is not in a transitionable state")
            connection.execute(
                """UPDATE installation_jobs SET status = ?, receipt_json = ?, error = ?,
                   updated_at = ? WHERE id = ?""",
                (status, json.dumps(asdict(receipt)) if receipt else "", error[:500], self._now(), job_id),
            )
            connection.commit()

    def recover_interrupted(self) -> tuple[int, ...]:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id FROM installation_jobs WHERE status = 'running'"
            ).fetchall()
            ids = tuple(int(row[0]) for row in rows)
            if ids:
                connection.execute(
                    """UPDATE installation_jobs SET status = 'waiting_for_permission',
                       error = 'Interrupted setup needs receipt and target review', updated_at = ?
                       WHERE status = 'running'""",
                    (self._now(),),
                )
            connection.commit()
        return ids

    def run_next(self, execute: Callable[[InstallJob], InstallReceipt]) -> int | None:
        job = self.claim_next()
        if job is None:
            return None
        try:
            receipt = execute(job)
        except Exception as exc:
            self.transition(job.id, "failed", error=str(exc))
            return job.id
        self.transition(job.id, "completed", receipt=receipt)
        return job.id
