from local_model_chat.inference.capacity import capacity_snapshot
from local_model_chat.inference.registry import OfferingRecord


def _offering(kind: str, quota: dict) -> OfferingRecord:
    return OfferingRecord(
        "route", "provider", "model", "direct", None, kind, "eligible",
        {}, "private", True, None, None, quota, None, None, "", "", "healthy",
    )


def test_subscription_allowance_without_provider_evidence_remains_unknown() -> None:
    offering = _offering("subscription", {"remaining": 100, "reset_at": "2026-11-01"})
    snapshot = capacity_snapshot(offering)

    assert snapshot.remaining is None
    assert snapshot.accuracy == "unknown"
    assert snapshot.reset_at is None


def test_provider_reported_free_allowance_carries_timezone_and_source() -> None:
    offering = _offering("recurring_free", {
        "remaining": 25, "unit": "requests", "source_type": "provider_reported",
        "source": "provider account dashboard", "observed_at": "2026-10-15T12:00:00+00:00",
        "reset_at": "2026-11-01T00:00:00+00:00",
    })
    snapshot = capacity_snapshot(offering)

    assert snapshot.remaining == 25
    assert snapshot.accuracy == "provider_reported"
    assert snapshot.reset_at == "2026-11-01T00:00:00+00:00"
    assert snapshot.source == "provider account dashboard"


def test_metered_route_reports_budget_rather_than_quota() -> None:
    assert capacity_snapshot(_offering("metered", {"remaining": 99})).accuracy == "budget_only"
