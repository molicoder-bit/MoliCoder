"""Offline comparison of quoted-price and measured-outcome routing.

The caller supplies independent training and evaluation observations. This module
never invokes an offering and keeps missing spend distinct from zero spend.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import isfinite

from .registry import ProviderRegistry
from .router import RouteMetrics, RouteRequest, choose_route


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
