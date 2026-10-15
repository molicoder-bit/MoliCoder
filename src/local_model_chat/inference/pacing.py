from __future__ import annotations

import calendar
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from ..storage import ChatStore


@dataclass(frozen=True, slots=True)
class PacingSnapshot:
    project_id: int
    month: str
    monthly_limit_usd: float | None
    committed_usd: float
    projected_month_usd: float | None
    state: str
    explanation: str


def project_pacing(store: ChatStore, project_id: int, *, now: datetime | None = None) -> PacingSnapshot:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("pacing time needs a timezone")
    month = now.strftime("%Y-%m")
    with closing(store._connect()) as connection:
        limit_row = connection.execute(
            """SELECT limit_usd FROM budget_scope_limits
               WHERE project_id = ? AND kind = 'month' AND scope = ?""",
            (project_id, month),
        ).fetchone()
        committed = float(connection.execute(
            """SELECT COALESCE(SUM(CASE WHEN status = 'active' THEN amount_usd
                   WHEN status = 'settled' THEN actual_usd ELSE 0 END), 0)
               FROM budget_reservations
               WHERE project_id = ? AND substr(created_at, 1, 7) = ?""",
            (project_id, month),
        ).fetchone()[0])
        latest = connection.execute(
            """SELECT r.offering_id, e.status, e.source_event, e.estimated_cost_usd,
                      e.actual_cost_usd, e.cost_accuracy
               FROM inference_requests r
               JOIN inference_usage_events e ON e.request_id = r.request_id
               WHERE r.project_id = ?
               ORDER BY e.id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
    latest_explanation = ""
    if latest is not None:
        if latest["status"] == "denied":
            reason = latest["source_event"].removeprefix("policy:")
            latest_explanation = f" Latest request deferred before dispatch: {reason}."
        elif latest["status"] == "completed":
            price = (
                "local zero provider cost" if latest["cost_accuracy"] == "local_zero"
                else f"estimated ${latest['estimated_cost_usd']:.4f}" if latest["estimated_cost_usd"] is not None
                else "cost unknown"
            )
            latest_explanation = f" Latest request passed policy and completed ({price})."
        elif latest["status"] == "started":
            latest_explanation = " Latest request passed policy and is running."
    if limit_row is None:
        return PacingSnapshot(
            project_id, month, None, committed, None, "unconfigured",
            "No monthly paid API limit is configured; paid routes remain subject to the project hard limit and authorization."
            + latest_explanation,
        )
    limit = float(limit_row[0])
    days = calendar.monthrange(now.year, now.month)[1]
    elapsed = (now.day - 1) + (now.hour + now.minute / 60) / 24
    projected = committed * days / max(elapsed, 0.25)
    state = "fast_burn" if projected > limit and committed > 0 else "on_pace"
    explanation = (
        f"{state.replace('_', ' ')}: ${committed:.4f} reserved or conservatively settled "
        f"of ${limit:.4f} for {month} UTC; straight-line projection ${projected:.4f}. "
        "This is a pacing estimate, not a provider-confirmed balance." + latest_explanation
    )
    return PacingSnapshot(project_id, month, limit, committed, projected, state, explanation)
