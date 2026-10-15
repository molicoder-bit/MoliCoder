"""Offline comparison of quoted-price and measured-outcome routing.

The caller supplies independent training and evaluation observations. This module
never invokes an offering and keeps missing spend distinct from zero spend.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from .registry import ProviderRegistry
from .router import RouteMetrics, RouteRequest, choose_route
from ..storage import ChatStore


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    scenario_id: str
    offering_id: str
    accepted: bool
    seconds: float
    cost_usd: float | None
    retries: int = 0
    interventions: int = 0
    failed: bool = False

    def __post_init__(self) -> None:
        if not self.scenario_id or not self.offering_id:
            raise ValueError("outcome needs a scenario and offering")
        if not isfinite(self.seconds) or self.seconds < 0:
            raise ValueError("outcome duration must be finite and nonnegative")
        if self.cost_usd is not None and (not isfinite(self.cost_usd) or self.cost_usd < 0):
            raise ValueError("outcome cost must be finite and nonnegative")
        if self.retries < 0 or self.interventions < 0:
            raise ValueError("outcome counts cannot be negative")


@dataclass(frozen=True, slots=True)
class PolicySummary:
    scenarios: int
    accepted: int
    failures: int
    interventions: int
    paid_uses: int
    total_seconds: float
    known_cost_usd: float
    unknown_costs: int
    cost_per_accepted_usd: float | None
    seconds_per_accepted: float | None


@dataclass(frozen=True, slots=True)
class RoutingComparison:
    baseline: PolicySummary
    tuned: PolicySummary
    selections: tuple[tuple[str, str, str], ...]


def measured_metrics(outcomes: tuple[RoutingOutcome, ...]) -> dict[str, RouteMetrics]:
    grouped: dict[str, list[RoutingOutcome]] = defaultdict(list)
    for outcome in outcomes:
        grouped[outcome.offering_id].append(outcome)
    return {
        offering_id: RouteMetrics(
            sum(item.accepted for item in rows) / len(rows),
            sum(item.retries for item in rows) / len(rows),
            sum(item.seconds for item in rows) / len(rows),
            len(rows),
        )
        for offering_id, rows in grouped.items()
    }


def recorded_task_outcomes(store: ChatStore, project_id: int) -> tuple[RoutingOutcome, ...]:
    """Read Build outcomes only when task, route, and spend attribution are clear.

    Opaque calls, multiple offerings, incomplete usage events, and tasks without a
    timed start are omitted. Unknown cost stays unknown. No prompt is loaded.
    """
    with closing(store._connect()) as connection:
        tasks = connection.execute(
            """SELECT t.id, t.run_id, t.status, t.attempts, t.updated_at
               FROM tasks t JOIN runs r ON r.id = t.run_id
               WHERE r.project_id = ? AND t.status IN
                   ('completed', 'needs_attention', 'review_required', 'blocked', 'cancelled')
               ORDER BY t.run_id, t.id""",
            (project_id,),
        ).fetchall()
        outcomes: list[RoutingOutcome] = []
        for task in tasks:
            if connection.execute(
                """SELECT 1 FROM external_calls
                   WHERE task_id = ? OR (run_id = ? AND task_id IS NULL) LIMIT 1""",
                (task["id"], task["run_id"]),
            ).fetchone():
                continue
            requests = connection.execute(
                """SELECT r.offering_id, e.status, e.actual_cost_usd
                   FROM inference_requests r
                   LEFT JOIN inference_usage_events e ON e.id = (
                       SELECT MAX(id) FROM inference_usage_events
                       WHERE request_id = r.request_id
                         AND status IN ('completed', 'failed', 'cancelled', 'denied')
                   )
                   WHERE r.task_id = ? ORDER BY r.created_at""",
                (task["id"],),
            ).fetchall()
            if not requests or any(row["status"] is None for row in requests):
                continue
            offerings = {row["offering_id"] for row in requests}
            if len(offerings) != 1:
                continue
            started = connection.execute(
                """SELECT created_at FROM events
                   WHERE task_id = ? AND kind = 'task_started'
                   ORDER BY id LIMIT 1""", (task["id"],),
            ).fetchone()
            if started is None:
                continue
            seconds = (datetime.fromisoformat(task["updated_at"])
                       - datetime.fromisoformat(started[0])).total_seconds()
            if seconds < 0:
                continue
            completion = store.get_task_completion(task["id"])
            accepted = (task["status"] == "completed" and completion is not None
                        and not completion.unmet_criteria
                        and completion.human_review_status in {"not_required", "approved"})
            interventions = connection.execute(
                """SELECT COUNT(*) FROM events WHERE task_id = ?
                   AND kind IN ('task_review_required', 'provider_takeover')""",
                (task["id"],),
            ).fetchone()[0]
            known_cost = all(row["actual_cost_usd"] is not None for row in requests)
            outcomes.append(RoutingOutcome(
                f"run:{task['run_id']}:task:{task['id']}", next(iter(offerings)),
                accepted, seconds,
                sum(row["actual_cost_usd"] for row in requests) if known_cost else None,
                max(0, task["attempts"] - 1), interventions,
                task["status"] != "completed" or any(row["status"] != "completed" for row in requests),
            ))
    return tuple(outcomes)


def _summarize(rows: list[RoutingOutcome], paid_ids: set[str]) -> PolicySummary:
    accepted = sum(item.accepted for item in rows)
    unknown = sum(item.cost_usd is None for item in rows)
    return PolicySummary(
        len(rows), accepted, sum(item.failed for item in rows),
        sum(item.interventions for item in rows),
        sum(item.offering_id in paid_ids for item in rows),
        sum(item.seconds for item in rows),
        sum(item.cost_usd for item in rows if item.cost_usd is not None),
        unknown,
        (sum(item.cost_usd for item in rows if item.cost_usd is not None) / accepted
         if accepted and not unknown else None),
        sum(item.seconds for item in rows) / accepted if accepted else None,
    )


def compare_routing(
    registry: ProviderRegistry,
    scenarios: tuple[tuple[str, RouteRequest], ...],
    training: tuple[RoutingOutcome, ...],
    evaluation: tuple[RoutingOutcome, ...],
) -> RoutingComparison:
    """Replay both policies on held-out, matched outcomes and current eligibility.

    Baseline uses the published token price with perfect-success assumptions.
    Tuned routing uses training acceptance, retry, and latency observations. A
    missing held-out result or a route blocked by today's policy makes the
    comparison incomplete and raises instead of inventing a measurement.
    """
    scenario_ids = [key for key, _ in scenarios]
    if not scenarios or len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("evaluation scenarios must be nonempty and unique")
    if {item.scenario_id for item in evaluation} & {item.scenario_id for item in training}:
        raise ValueError("training and evaluation scenarios must be disjoint")
    observed = {(item.scenario_id, item.offering_id): item for item in evaluation}
    if len(observed) != len(evaluation):
        raise ValueError("duplicate held-out outcome")
    offerings = {item.id: item for item in registry.list_offerings()}
    baseline_metrics = {key: RouteMetrics(1, 0, 0, 5) for key in offerings}
    tuned_metrics = measured_metrics(training)
    baseline_rows: list[RoutingOutcome] = []
    tuned_rows: list[RoutingOutcome] = []
    selections: list[tuple[str, str, str]] = []
    for scenario_id, request in scenarios:
        baseline = choose_route(registry, request, metrics=baseline_metrics)
        tuned = choose_route(registry, request, metrics=tuned_metrics)
        if baseline.kind != "offering" or tuned.kind != "offering":
            raise ValueError(f"scenario {scenario_id} has no comparable eligible routes")
        for decision, rows in ((baseline, baseline_rows), (tuned, tuned_rows)):
            outcome = observed.get((scenario_id, decision.offering_id))
            if outcome is None:
                raise ValueError(f"missing held-out outcome for {scenario_id} and {decision.offering_id}")
            rows.append(outcome)
        selections.append((scenario_id, baseline.offering_id, tuned.offering_id))
    paid_ids = {key for key, item in offerings.items() if item.offering_class in {"trial", "metered"}}
    return RoutingComparison(
        _summarize(baseline_rows, paid_ids), _summarize(tuned_rows, paid_ids), tuple(selections),
    )
