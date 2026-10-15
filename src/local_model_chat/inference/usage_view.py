from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json

from ..storage import ChatStore
from .capacity import capacity_snapshot
from .pacing import project_pacing
from .registry import ProviderRegistry


def render_usage_report(
    store: ChatStore, registry: ProviderRegistry, project_id: int, *, now: datetime | None = None,
) -> str:
    """Show recorded usage with its source and unknown costs kept separate."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("usage report time needs a timezone")
    now = now.astimezone(timezone.utc)
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    with closing(store._connect()) as connection:
        rows = connection.execute(
            """SELECT r.created_at, r.offering_id, e.status, e.input_tokens, e.output_tokens,
                      e.actual_cost_usd, e.estimated_cost_usd, e.cost_accuracy, e.source_event
               FROM inference_requests r
               LEFT JOIN inference_usage_events e ON e.id = (
                   SELECT MAX(id) FROM inference_usage_events WHERE request_id = r.request_id
               ) WHERE r.project_id = ? ORDER BY r.created_at""",
            (project_id,),
        ).fetchall()
        project_limit = connection.execute(
            "SELECT limit_usd, premium_reserve_usd FROM project_budget_limits WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        task_limits = connection.execute(
            """SELECT scope, limit_usd FROM budget_scope_limits
               WHERE project_id = ? AND kind = 'task' ORDER BY scope""", (project_id,),
        ).fetchall()
        opaque_calls = connection.execute(
            """SELECT COUNT(*) FROM external_calls c JOIN runs r ON r.id = c.run_id
               WHERE r.project_id = ?""", (project_id,),
        ).fetchone()[0]
        route_row = connection.execute(
            """SELECT e.payload_json FROM events e JOIN runs r ON r.id = e.run_id
               WHERE r.project_id = ? AND e.kind = 'routing_decision'
               ORDER BY e.id DESC LIMIT 1""", (project_id,),
        ).fetchone()
    lines = [f"Usage and budgets · {day} UTC", "Recorded API requests; provider subscription balances may be unavailable."]
    for label, prefix in (("Today", day), ("This month", month)):
        selected = [row for row in rows if row["created_at"].startswith(prefix)]
        actual = sum(row["actual_cost_usd"] for row in selected if row["actual_cost_usd"] is not None)
        estimated = sum(row["estimated_cost_usd"] for row in selected
                        if row["actual_cost_usd"] is None and row["estimated_cost_usd"] is not None)
        unknown = sum(row["actual_cost_usd"] is None and row["estimated_cost_usd"] is None for row in selected)
        sources = sorted({row["cost_accuracy"] or "unknown" for row in selected})
        lines.append(
            f"{label}: {len(selected)} requests · reported/local cost ${actual:.4f} · "
            f"estimate ${estimated:.4f} · {unknown} unknown cost · "
            f"{sum(row['input_tokens'] or 0 for row in selected)} input / "
            f"{sum(row['output_tokens'] or 0 for row in selected)} output tokens · "
            f"accuracy: {', '.join(sources) if sources else 'no records'}"
        )
    lines.append(
        f"Project hard API limit: ${project_limit['limit_usd']:.4f}; "
        f"premium reserve ${project_limit['premium_reserve_usd']:.4f}"
        if project_limit else "Project hard API limit: unconfigured"
    )
    pacing = project_pacing(store, project_id, now=now)
    lines.append(f"Monthly pacing: {pacing.explanation}")
    lines.append("Task API limits: " + (
        ", ".join(f"{row['scope']} ${row['limit_usd']:.4f}" for row in task_limits)
        if task_limits else "none configured"
    ))
    lines.append(f"Opaque subscription CLI calls: {opaque_calls}; internal usage and cost unknown")
    lines.append("Accepted-task cost: unknown until usage and acceptance are linked to a measured task outcome")
    if route_row:
        route = json.loads(route_row[0])
        lines.append(f"Latest Build route: {route.get('selected', 'unknown')} — {route.get('reason', 'reason unknown')}")
        lines.append("Route limits: " + "; ".join(route.get("constraints", ())))
    else:
        lines.append("Latest Build route: no recorded decision")
    lines.append("Provider balances and reset dates:")
    for offering in registry.list_offerings():
        snapshot = capacity_snapshot(offering)
        remaining = (f"{snapshot.remaining:g} {snapshot.unit}" if snapshot.remaining is not None
                     else "unknown")
        lines.append(
            f"  {offering.id}: {remaining}; reset {snapshot.reset_at or 'unknown'}; "
            f"source {snapshot.source or snapshot.accuracy}; observed {snapshot.observed_at or 'unknown'}"
        )
    return "\n".join(lines)
