from dataclasses import replace
from datetime import datetime, timedelta, timezone

from local_model_chat.inference.adapters.ollama import ensure_local_offering, local_offering_id
from local_model_chat.environment.security import platform_security
from local_model_chat.inference import eligibility
from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.inference.registry import OfferingRecord, ProviderRegistry
from local_model_chat.inference.router import (
    RouteMetrics, RouteRequest, _expected_accepted_cost, choose_route,
)
from local_model_chat.storage import ChatStore


import pytest


@pytest.fixture(autouse=True)
def _metered_fixture_has_verified_windows_backend(monkeypatch):
    monkeypatch.setattr(eligibility, "platform_security", lambda: platform_security("Windows"))


def _registry(tmp_path) -> ProviderRegistry:
    registry = ProviderRegistry(ChatStore(tmp_path / "router.sqlite3"))
    ensure_local_offering(registry, "coder:latest", context_window=4096, max_output_tokens=1000)
    registry.set_offering_state(
        local_offering_id("coder:latest"), status="eligible", health_state="healthy",
        actor="test", reason="local model available",
    )
    return registry


def test_deterministic_read_and_uncertain_intent_do_not_select_paid_route(tmp_path) -> None:
    registry = _registry(tmp_path)
    assert choose_route(registry, RouteRequest("What task are we on?", None)).kind == "deterministic"
    uncertain = choose_route(registry, RouteRequest("something about that", None))
    assert uncertain.kind == "clarify"
    assert uncertain.offering_id is None


def test_verified_local_capability_selected_and_missing_capability_clarifies(tmp_path) -> None:
    registry = _registry(tmp_path)
    chosen = choose_route(registry, RouteRequest("Evaluate this change", None))
    assert chosen.offering_id == local_offering_id("coder:latest")
    blocked = choose_route(registry, RouteRequest(
        "Evaluate this change", None, required_capabilities=("structured_output",),
    ))
    assert blocked.kind == "clarify"
    assert "missing capability" in " ".join(blocked.alternatives)


def test_accepted_task_score_uses_price_success_retries_latency_and_urgency() -> None:
    offering = OfferingRecord(
        "paid", "host", "model", "direct", None, "metered", "eligible",
        {}, "private", True, 1.0, 2.0, {}, None, None, "", "", "healthy",
    )
    request = RouteRequest("Evaluate this", None, max_input_tokens=1000, max_output_tokens=500)
    metrics = RouteMetrics(0.5, 1, 10, 10)
    assert _expected_accepted_cost(offering, request, metrics) == 0.008
    urgent = replace(request, urgency=1)
    assert _expected_accepted_cost(offering, urgent, metrics) > 0.008
    assert _expected_accepted_cost(offering, request, RouteMetrics(0.5, 1, 10, 2)) is None


def test_ranked_route_uses_measured_accepted_cost_among_approved_offerings(tmp_path) -> None:
    store = ChatStore(tmp_path / "hosted-router.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    registry = ProviderRegistry(store)
    registry.add_provider("host", "Host", "https://host.example")
    registry.add_model("coder", "coder", "v1")
    now = datetime.now(timezone.utc).isoformat()
    for offering_id, price in (("cheap-raw", 0.5), ("better-accepted", 1.0)):
        registry.add_offering(offering_id, "host", "coder", offering_id, "metered")
        import_catalog_record(store, CatalogRecord(
            offering_id, "https://host.example/prices", now,
            "https://host.example/terms",
            {"stream": True, "context_window": 4096, "max_output_tokens": 1000},
            "private", True, price, price, {},
        ))
        registry.set_offering_state(
            offering_id, status="eligible", health_state="healthy", actor="test", reason="fixture",
        )
        store.record_action_authorization(
            project_id=project.id, action="inference_route", scope=offering_id,
            risk="financial", decision="approved", actor="user",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )

    decision = choose_route(
        registry, RouteRequest("Evaluate this code", project.id),
        metrics={
            "cheap-raw": RouteMetrics(0.2, 1, 30, 10),
            "better-accepted": RouteMetrics(0.9, 0, 10, 10),
        },
    )

    assert decision.offering_id == "better-accepted"
    assert "cheap-raw" in decision.alternatives
