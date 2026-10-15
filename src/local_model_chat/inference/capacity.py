from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .registry import OfferingRecord


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    offering_id: str
    kind: str
    remaining: float | None
    unit: str
    reset_at: str | None
    observed_at: str | None
    source: str | None
    accuracy: str


def _aware(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp.isoformat() if stamp.tzinfo is not None else None


def capacity_snapshot(offering: OfferingRecord) -> CapacitySnapshot:
    """Report only provider-sourced allowance as confirmed; keep unknowns unknown."""
    kind = offering.offering_class
    if kind == "local":
        return CapacitySnapshot(offering.id, kind, None, "local", None, None, None, "local_unmetered")
    if kind == "metered":
        return CapacitySnapshot(offering.id, kind, None, "USD", None, None, None, "budget_only")
    quota = offering.quota
    observed_at = _aware(quota.get("observed_at"))
    reset_at = _aware(quota.get("reset_at"))
    source = quota.get("source") if isinstance(quota.get("source"), str) else None
    confirmed = quota.get("source_type") == "provider_reported" and bool(source and observed_at)
    remaining = quota.get("remaining")
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or remaining < 0:
        remaining = None
    if not confirmed:
        remaining = None
    unit = str(quota.get("unit") or "requests")[:40]
    return CapacitySnapshot(
        offering.id, kind, float(remaining) if remaining is not None else None,
        unit, reset_at, observed_at, source,
        "provider_reported" if confirmed else "unknown",
    )
