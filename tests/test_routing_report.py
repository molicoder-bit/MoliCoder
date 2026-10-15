from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.inference.adapters.ollama import ensure_local_offering, local_offering_id
from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
from local_model_chat.inference.ledger import InferenceLedger, UsageEvent
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.inference.router import RouteRequest
from local_model_chat.inference.routing_report import (
    RoutingOutcome, compare_routing, recorded_task_outcomes,
)
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


def test_recorded_task_outcomes_require_unambiguous_attribution(tmp_path):
    store = ChatStore(tmp_path / "recorded.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    registry = ProviderRegistry(store)
    ensure_local_offering(registry, "test:latest", context_window=4096, max_output_tokens=500)
    run_id = store.create_run(project.id, "Feature", "LOCAL_OFFLINE")
    task_id = store.create_run_task(run_id, "step", "Step", "Edit", "low")
    store.add_event(run_id, "task_started", {}, task_id)
    ledger = InferenceLedger(store)
    ledger.start(InferenceRequest(
        "task-call", project.id, local_offering_id("test:latest"), "test:latest",
        InferencePurpose.EXECUTION, ({"role": "user", "content": "edit"},),
        "private", (), 10, 10, 60, run_id=run_id, task_id=task_id,
    ))
    ledger.append(UsageEvent("task-call", "completed", 10, 2,
                             actual_cost_usd=0, cost_accuracy="local_zero"))
    store.record_task_completion(
        run_id, task_id, revision="abc123", checks=("test",),
        validation_summary="validation passed", output_artifacts=("file.py",),
        unmet_criteria=(), human_review_status="not_required",
    )
    store.update_run_task(task_id, "completed", 1)
    outcomes = recorded_task_outcomes(store, project.id)
    assert len(outcomes) == 1
    assert outcomes[0].accepted is True
    assert outcomes[0].cost_usd == 0
    assert outcomes[0].offering_id == local_offering_id("test:latest")

    # A task that also used an opaque provider cannot be assigned this local cost.
    store.add_external_call(run_id, "opaque", "execution", "fixture", 0, 0, 1, "completed", task_id)
    assert recorded_task_outcomes(store, project.id) == ()
