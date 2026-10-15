from __future__ import annotations

from dataclasses import replace

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from .eligibility import EligibilityRequest, evaluate_eligibility
from .registry import ProviderRegistry


def record_health(
    registry: ProviderRegistry, offering_id: str, state: str, *, actor: str, reason: str,
) -> None:
    offering = registry.get_offering(offering_id)
    if offering is None:
        raise ValueError("offering does not exist")
    status = "unavailable" if state == "unavailable" else None
    registry.set_offering_state(
        offering_id, status=status, health_state=state, actor=actor, reason=reason,
    )


def enable_offering(
    registry: ProviderRegistry, offering_id: str, request: EligibilityRequest,
    *, project_id: int | None, actor: str,
) -> None:
    offering = registry.get_offering(offering_id)
    if offering is None:
        raise ValueError("offering does not exist")
    if offering.status == "deprecated":
        raise ValueError("deprecated offering cannot be re-enabled")
    risk = ActionRisk.FINANCIAL if offering.offering_class in {"metered", "trial"} else ActionRisk.EXTERNAL
    AuthorizationPolicy(registry.store).require(ActionRequest(
        project_id, "enable_offering", offering_id, risk,
    ))
    decision = evaluate_eligibility(replace(offering, status="eligible"), request, registry)
    if not decision.eligible:
        raise ValueError("offering is not eligible: " + "; ".join(decision.reasons))
    registry.set_offering_state(
        offering_id, status="eligible", actor=actor, reason="explicitly enabled after eligibility check",
    )


def disable_offering(registry: ProviderRegistry, offering_id: str, *, actor: str, reason: str) -> None:
    registry.set_offering_state(offering_id, status="disabled", actor=actor, reason=reason)


def deprecate_offering(registry: ProviderRegistry, offering_id: str, *, actor: str, reason: str) -> None:
    registry.set_offering_state(offering_id, status="deprecated", actor=actor, reason=reason)
