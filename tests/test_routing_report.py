from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.inference.router import RouteRequest
from local_model_chat.inference.routing_report import RoutingOutcome, compare_routing
from local_model_chat.storage import ChatStore


def _registry(tmp_path):
    store = ChatStore(tmp_path / "routing-report.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    registry = ProviderRegistry(store)
    registry.add_provider("host", "Host", "https://host.example")
    registry.add_model("coder", "coder", "v1")
    now = datetime.now(timezone.utc).isoformat()
    for offering_id, price in (("cheap", 0.5), ("reliable", 1.0)):
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
    return registry, project.id


def test_held_out_comparison_reports_accepted_cost_latency_and_failures(tmp_path):
    registry, project_id = _registry(tmp_path)
    training = tuple(
        RoutingOutcome(f"train-{i}", "cheap", i == 0, 30, 0.0005, 1)
        for i in range(5)
    ) + tuple(
        RoutingOutcome(f"train-{i}", "reliable", True, 10, 0.001)
        for i in range(5)
    )
    evaluation = (
        RoutingOutcome("eval", "cheap", False, 32, 0.0005, 1, 1, True),
        RoutingOutcome("eval", "reliable", True, 11, 0.001),
    )
    request = RouteRequest("Evaluate this code", project_id)
    report = compare_routing(registry, (("eval", request),), training, evaluation)
    assert report.selections == (("eval", "cheap", "reliable"),)
    assert report.baseline.failures == 1
    assert report.baseline.interventions == 1
    assert report.baseline.cost_per_accepted_usd is None
    assert report.tuned.cost_per_accepted_usd == 0.001
    assert report.tuned.seconds_per_accepted == 11
    assert report.tuned.paid_uses == 1


def test_comparison_preserves_privacy_and_does_not_guess_missing_cost(tmp_path):
    registry, project_id = _registry(tmp_path)
    training = tuple(
        RoutingOutcome(f"train-{i}", offering, True, 5, None)
        for i, offering in enumerate(["cheap"] * 5 + ["reliable"] * 5)
    )
    request = RouteRequest("Evaluate this code", project_id, sensitivity="restricted")
    with pytest.raises(ValueError, match="no comparable eligible routes"):
        compare_routing(registry, (("eval", request),), training, ())
    request = replace(request, sensitivity="private")
    evaluation = (
        RoutingOutcome("eval", "cheap", True, 5, None),
        RoutingOutcome("eval", "reliable", True, 5, None),
    )
    report = compare_routing(registry, (("eval", request),), training, evaluation)
    assert report.baseline.unknown_costs == 1
    assert report.tuned.cost_per_accepted_usd is None
    with pytest.raises(ValueError, match="disjoint"):
        compare_routing(registry, (("train-0", request),), training,
                        (replace(evaluation[0], scenario_id="train-0"),))
