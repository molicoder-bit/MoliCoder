from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..storage import ChatStore


class BudgetDenied(PermissionError):
    pass


class BudgetManager:
    """Project hard limits with serialized, request-bound reservations."""

    def __init__(self, store: ChatStore) -> None:
        self.store = store

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def set_project_limit(
        self, project_id: int, limit_usd: float, *, actor: str, premium_reserve_usd: float = 0,
    ) -> None:
        if (actor != "user" or not 0 <= limit_usd < float("inf")
                or not 0 <= premium_reserve_usd <= limit_usd):
            raise ValueError("a finite, explicit user-set project limit is required")
        with closing(self.store._connect()) as connection:
            connection.execute(
                """INSERT INTO project_budget_limits(project_id, limit_usd, premium_reserve_usd, updated_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET
                   limit_usd = excluded.limit_usd,
                   premium_reserve_usd = excluded.premium_reserve_usd,
                   updated_at = excluded.updated_at""",
                (project_id, limit_usd, premium_reserve_usd, self._now()),
            )
            connection.commit()

    def set_scope_limit(
        self, project_id: int, kind: str, scope: str, limit_usd: float, *, actor: str,
    ) -> None:
        if (actor != "user" or kind not in {"task", "month"} or not scope
                or not 0 <= limit_usd < float("inf")):
            raise ValueError("a finite, explicit user-set task or month limit is required")
        with closing(self.store._connect()) as connection:
            connection.execute(
                """INSERT INTO budget_scope_limits(project_id, kind, scope, limit_usd, updated_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id, kind, scope) DO UPDATE SET
                   limit_usd = excluded.limit_usd, updated_at = excluded.updated_at""",
                (project_id, kind, scope, limit_usd, self._now()),
            )
            connection.commit()

    def reserve(
        self, request_id: str, project_id: int, amount_usd: float, *,
        task_scope: str = "", tier: str = "standard", allow_exception: bool = False,
    ) -> str:
        if not request_id or not 0 < amount_usd < float("inf"):
            raise ValueError("request ID and finite positive worst-case cost are required")
        if tier not in {"standard", "premium"}:
            raise ValueError("unknown budget tier")
        now = self._now()
        month = now[:7]
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            limit = connection.execute(
                "SELECT limit_usd, premium_reserve_usd FROM project_budget_limits WHERE project_id = ?", (project_id,),
            ).fetchone()
            if limit is None:
                raise BudgetDenied("project has no configured hard budget")
            used = connection.execute(
                """SELECT COALESCE(SUM(CASE WHEN status = 'active' THEN amount_usd
                       WHEN status = 'settled' THEN actual_usd ELSE 0 END), 0)
                   FROM budget_reservations WHERE project_id = ?""",
                (project_id,),
            ).fetchone()[0]
            reasons = []
            project_cap = limit[0] - (limit[1] if tier == "standard" else 0)
            if used + amount_usd > project_cap + 1e-10:
                reasons.append("project hard budget or premium reserve would be exceeded")
            scoped_limits = connection.execute(
                "SELECT kind, scope, limit_usd FROM budget_scope_limits WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            for row in scoped_limits:
                kind, scope, cap = row
                if kind == "task":
                    if not task_scope:
                        reasons.append("task budget needs a task scope")
                        continue
                    if scope not in {"*", task_scope}:
                        continue
                    clause, parameters = "AND task_scope = ?", (task_scope,)
                else:
                    if scope != month:
                        continue
                    clause, parameters = "AND substr(created_at, 1, 7) = ?", (month,)
                scoped_used = connection.execute(
                    """SELECT COALESCE(SUM(CASE WHEN status = 'active' THEN amount_usd
                           WHEN status = 'settled' THEN actual_usd ELSE 0 END), 0)
                       FROM budget_reservations WHERE project_id = ? """ + clause,
                    (project_id, *parameters),
                ).fetchone()[0]
                if scoped_used + amount_usd > cap + 1e-10:
                    reasons.append(f"{kind} hard budget would be exceeded")
            if reasons:
                if not allow_exception:
                    raise BudgetDenied("; ".join(reasons))
                AuthorizationPolicy(self.store).require(ActionRequest(
                    project_id, "budget_exception", f"request:{request_id}:project:{project_id}",
                    ActionRisk.FINANCIAL,
                ))
            reservation_id = str(uuid4())
            connection.execute(
                """INSERT INTO budget_reservations(
                    id, request_id, project_id, task_scope, tier, amount_usd,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (reservation_id, request_id, project_id, task_scope, tier, amount_usd, now, now),
            )
            connection.commit()
        return reservation_id

    def active_amount(self, reservation_id: str, request_id: str, project_id: int | None) -> float | None:
        if project_id is None:
            return None
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                """SELECT amount_usd FROM budget_reservations
                   WHERE id = ? AND request_id = ? AND project_id = ? AND status = 'active'""",
                (reservation_id, request_id, project_id),
            ).fetchone()
        return float(row[0]) if row is not None else None

    def settle(self, reservation_id: str, actual_usd: float | None = None) -> None:
        if actual_usd is not None and not 0 <= actual_usd < float("inf"):
            raise ValueError("actual cost must be finite and nonnegative")
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT amount_usd FROM budget_reservations WHERE id = ? AND status = 'active'",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise BudgetDenied("reservation is not active")
            connection.execute(
                """UPDATE budget_reservations SET status = 'settled', actual_usd = ?,
                   updated_at = ? WHERE id = ?""",
                (row[0] if actual_usd is None else actual_usd, self._now(), reservation_id),
            )
            connection.commit()

    def release(self, reservation_id: str) -> None:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE budget_reservations SET status = 'released', updated_at = ?
                   WHERE id = ? AND status = 'active'""",
                (self._now(), reservation_id),
            )
            if cursor.rowcount != 1:
                raise BudgetDenied("reservation is not active")
            connection.commit()
