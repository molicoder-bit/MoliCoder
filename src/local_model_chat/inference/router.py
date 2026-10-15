from __future__ import annotations

from dataclasses import dataclass

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..intent import IntentDecision, IntentKind, classify_intent
from .eligibility import EligibilityRequest, evaluate_eligibility
from .registry import OfferingRecord, ProviderRegistry


@dataclass(frozen=True, slots=True)
class RouteRequest:
    message: str
    project_id: int | None
    sensitivity: str = "private"
    required_capabilities: tuple[str, ...] = ()
    max_input_tokens: int = 1024
    max_output_tokens: int = 512
    urgency: float = 0.0
    explicit_intent: IntentKind | None = None
    candidate_offering_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteMetrics:
    accepted_fraction: float
    mean_retries: float
    mean_latency_seconds: float
    samples: int


@dataclass(frozen=True, slots=True)
class RouteDecision:
    kind: str
    offering_id: str | None
    reason: str
    alternatives: tuple[str, ...] = ()


def required_for_intent(intent: IntentDecision) -> tuple[str, ...]:
    if intent.kind in {IntentKind.TOOL, IntentKind.MUTATION, IntentKind.EXECUTION}:
        return ("tools",)
    return ("stream",)


def _expected_accepted_cost(
    offering: OfferingRecord, request: RouteRequest, metrics: RouteMetrics | None,
) -> float | None:
    if offering.offering_class == "local":
        base = 0.0
    elif offering.input_usd_per_million is not None and offering.output_usd_per_million is not None:
        base = (
            request.max_input_tokens * offering.input_usd_per_million
            + request.max_output_tokens * offering.output_usd_per_million
        ) / 1_000_000
    else:
        return None
    if metrics is None or metrics.samples < 5 or not 0 < metrics.accepted_fraction <= 1:
        return 0.0 if offering.offering_class == "local" else None
    if metrics.mean_retries < 0 or metrics.mean_latency_seconds < 0:
        return None
    failure_adjusted = base * (1 + metrics.mean_retries) / metrics.accepted_fraction
    latency_penalty = max(0.0, min(request.urgency, 1.0)) * metrics.mean_latency_seconds / 3600
    return failure_adjusted + latency_penalty


def choose_route(
    registry: ProviderRegistry, request: RouteRequest, *,
    metrics: dict[str, RouteMetrics] | None = None,
) -> RouteDecision:
    intent = (
        IntentDecision(request.explicit_intent, None, 1.0)
        if request.explicit_intent is not None else classify_intent(request.message)
    )
    if intent.kind == IntentKind.RETRIEVAL and intent.operation:
        return RouteDecision("deterministic", None, f"verified local read: {intent.operation}")
    if intent.confidence < 0.6:
        return RouteDecision("clarify", None, "task intent is uncertain; ask for a specific action")
    required = tuple(sorted(set(required_for_intent(intent)) | set(request.required_capabilities)))
    candidates: list[tuple[float, str]] = []
    excluded: list[str] = []
    for offering in registry.list_offerings():
        if request.candidate_offering_ids and offering.id not in request.candidate_offering_ids:
            continue
        paid = offering.offering_class in {"metered", "trial"}
        if offering.offering_class != "local":
            risk = ActionRisk.FINANCIAL if paid else ActionRisk.EXTERNAL
            approval = AuthorizationPolicy(registry.store).check(ActionRequest(
                request.project_id, "inference_route", offering.id, risk,
            ))
            if not approval.allowed:
                excluded.append(f"{offering.id}: route approval missing")
                continue
        eligibility = evaluate_eligibility(
            offering, EligibilityRequest(request.sensitivity, required, unattended=True, paid_allowed=paid),
            registry,
        )
        if not eligibility.eligible:
            excluded.append(f"{offering.id}: {'; '.join(eligibility.reasons)}")
            continue
        score = _expected_accepted_cost(offering, request, (metrics or {}).get(offering.id))
        if score is None:
            excluded.append(f"{offering.id}: accepted-task cost is unverified")
            continue
        candidates.append((score, offering.id))
    if not candidates:
        return RouteDecision("clarify", None, "no verified eligible route; select a local model or review provider constraints", tuple(excluded))
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected = candidates[0][1]
    return RouteDecision(
        "offering", selected,
        f"selected {selected} from eligible routes by estimated accepted-task cost",
        tuple(offering_id for _, offering_id in candidates[1:]) + tuple(excluded),
    )
