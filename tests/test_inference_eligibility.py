from dataclasses import replace
from datetime import datetime, timedelta, timezone

from local_model_chat.inference.eligibility import EligibilityRequest, evaluate_eligibility
from local_model_chat.inference.registry import OfferingRecord, ProviderRegistry
from local_model_chat.storage import ChatStore


def _offering() -> OfferingRecord:
    now = datetime.now(timezone.utc).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return OfferingRecord(
        "route", "provider", "model", "route", None, "recurring_free", "eligible",
        {"tools": True, "stream": True, "structured_output": True},
        "restricted", True, 0, 0, {
            "remaining": 10, "expires_at": future, "source_type": "provider_reported",
            "source": "provider account dashboard", "observed_at": now,
        },
        now, now, "https://provider.example/pricing", "https://provider.example/terms", "healthy",
    )


def test_eligibility_rejects_capability_privacy_automation_quota_and_health(tmp_path) -> None:
    registry = ProviderRegistry(ChatStore(tmp_path / "eligible.sqlite3"))
    request = EligibilityRequest("private", ("tools", "stream", "structured_output"), unattended=True)
    base = _offering()
    assert evaluate_eligibility(base, request, registry).eligible

    variants = (
        (replace(base, capabilities={"stream": True}), "missing capability"),
        (replace(base, privacy_class="public"), "privacy class"),
        (replace(base, automation_allowed=False), "unattended use"),
        (replace(base, quota={"remaining": 0}), "allowance is exhausted"),
        (replace(base, quota={"remaining": 10}), "allowance provenance is unverified"),
        (replace(base, quota={"remaining": 10, "expires_at": "2000-01-01T00:00:00+00:00"}), "observation expired"),
        (replace(base, health_state="unavailable"), "endpoint health"),
    )
    for offering, expected in variants:
        result = evaluate_eligibility(offering, request, registry)
        assert not result.eligible
        assert any(expected in reason for reason in result.reasons)


def test_metered_route_needs_fresh_known_price_and_paid_permission(tmp_path) -> None:
    registry = ProviderRegistry(ChatStore(tmp_path / "metered.sqlite3"))
    base = replace(_offering(), offering_class="metered", input_usd_per_million=None, output_usd_per_million=None)
    denied = evaluate_eligibility(base, EligibilityRequest("private"), registry)
    assert "price is unknown" in denied.reasons
    assert "paid capacity is not authorized" in denied.reasons
    old = replace(base, input_usd_per_million=1.0, output_usd_per_million=2.0,
                  price_checked_at="2000-01-01T00:00:00+00:00")
    assert "price is stale" in evaluate_eligibility(old, EligibilityRequest("private", paid_allowed=True), registry).reasons
