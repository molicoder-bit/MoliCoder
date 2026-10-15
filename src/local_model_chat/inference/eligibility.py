from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .registry import OfferingRecord, ProviderRegistry
from .capacity import capacity_snapshot
from ..environment.security import platform_security


_SENSITIVITY = {"public": 0, "private": 1, "restricted": 2}


@dataclass(frozen=True, slots=True)
class EligibilityRequest:
    sensitivity: str
    capabilities: tuple[str, ...] = ()
    unattended: bool = False
    paid_allowed: bool = False
    max_fact_age_days: int = 30


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[str, ...]


def _fresh(timestamp: str | None, max_age_days: int) -> bool:
    if not timestamp:
        return False
    try:
        checked = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return checked.tzinfo is not None and checked >= datetime.now(timezone.utc) - timedelta(days=max_age_days)


def evaluate_eligibility(
    offering: OfferingRecord, request: EligibilityRequest, registry: ProviderRegistry,
) -> EligibilityResult:
    reasons: list[str] = []
    if offering.status != "eligible":
        reasons.append(f"offering status is {offering.status}")
    if offering.health_state != "healthy":
        reasons.append(f"endpoint health is {offering.health_state}")
    for required in request.capabilities:
        if offering.capabilities.get(required) is not True:
            reasons.append(f"missing capability: {required}")
    if request.sensitivity not in _SENSITIVITY:
        reasons.append("unknown project sensitivity")
    elif offering.privacy_class not in _SENSITIVITY or _SENSITIVITY[offering.privacy_class] < _SENSITIVITY[request.sensitivity]:
        reasons.append("privacy class does not cover the project")
    if request.unattended and offering.offering_class != "local" and offering.automation_allowed is not True:
        reasons.append("unattended use is not verified as allowed")
    if offering.offering_class != "local" and not _fresh(offering.terms_checked_at, request.max_fact_age_days):
        reasons.append("terms are missing or stale")
    if offering.account_id is not None and registry.get_account_status(offering.account_id) != "ready":
        reasons.append("account authentication is not ready")
    if offering.offering_class in {"metered", "trial"}:
        if not platform_security().metered_api_allowed:
            reasons.append("metered API route needs a supported OS credential backend")
        if not request.paid_allowed:
            reasons.append("paid capacity is not authorized")
        if offering.input_usd_per_million is None or offering.output_usd_per_million is None:
            reasons.append("price is unknown")
        elif not _fresh(offering.price_checked_at, request.max_fact_age_days):
            reasons.append("price is stale")
    if offering.offering_class in {"recurring_free", "trial", "subscription"}:
        remaining = offering.quota.get("remaining")
        if remaining is None:
            reasons.append("remaining allowance is unknown")
        elif not isinstance(remaining, (int, float)) or remaining <= 0:
            reasons.append("allowance is exhausted")
        capacity = capacity_snapshot(offering)
        if capacity.accuracy != "provider_reported" or not _fresh(
            capacity.observed_at, request.max_fact_age_days,
        ):
            reasons.append("allowance provenance is unverified or stale")
        expires_at = offering.quota.get("expires_at")
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
            except ValueError:
                expiry = None
            if expiry is None or expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                reasons.append("allowance observation expired")
    return EligibilityResult(not reasons, tuple(reasons))
