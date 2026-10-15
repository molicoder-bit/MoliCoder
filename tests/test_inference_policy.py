from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.environment.security import platform_security
from local_model_chat.inference import eligibility
from local_model_chat.inference.budget import BudgetManager
from local_model_chat.inference.ledger import InferenceLedger
from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest, InferenceResult
from local_model_chat.inference.policy import ControlledGateway, InferencePolicy
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


@pytest.fixture(autouse=True)
def _metered_fixture_has_verified_windows_backend(monkeypatch):
    monkeypatch.setattr(eligibility, "platform_security", lambda: platform_security("Windows"))


class FakeAdapter:
    def __init__(self):
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        return InferenceResult(request.request_id, "completed", "ollama", request.model_id, "ok")


def _request(offering_id="local", model_id="coder"):
    return InferenceRequest(
        "request-1", 1, offering_id, model_id, InferencePurpose.CHAT,
        ({"role": "user", "content": "Hi"},), "private", ("stream",),
        100, 20, 30, stream=True,
    )


def test_policy_denies_before_adapter_and_allows_eligible_local_route(tmp_path) -> None:
    store = ChatStore(tmp_path / "policy.sqlite3")
    registry = ProviderRegistry(store)
    adapter = FakeAdapter()
    gateway = ControlledGateway(InferencePolicy(registry), adapter)
    with pytest.raises(PermissionError, match="unknown"):
        gateway.invoke(_request())
    assert adapter.calls == 0

    registry.add_provider("ollama", "Ollama", "https://ollama.com")
    registry.add_model("coder", "coder", "v1")
    registry.add_offering("local", "ollama", "coder", "loopback", "local")
    now = datetime.now(timezone.utc).isoformat()
    import_catalog_record(store, CatalogRecord(
        "local", "https://ollama.com", now, "", {
            "stream": True, "context_window": 4096, "max_output_tokens": 256,
        }, "restricted", None, None, None, {},
    ))
    registry.set_offering_state("local", status="eligible", health_state="healthy", actor="doctor", reason="local ready")

    assert gateway.invoke(_request()).content == "ok"
    assert adapter.calls == 1
    with pytest.raises(PermissionError, match="model does not match"):
        gateway.invoke(_request(model_id="other"))
    assert adapter.calls == 1
    with pytest.raises(PermissionError, match="reasoning effort"):
        gateway.invoke(replace(_request(), options={"reasoning_effort": "high"}))
    with pytest.raises(PermissionError, match="context window"):
        gateway.invoke(replace(_request(), options={"context_window": 100}))
    assert adapter.calls == 1
    import_catalog_record(store, CatalogRecord(
        "local", "https://ollama.com", now, "",
        {"stream": True, "context_window": 4096, "max_output_tokens": 256,
         "reasoning_effort_levels": "low,high"},
        "restricted", None, None, None, {},
    ))
    assert gateway.invoke(replace(_request(), options={"reasoning_effort": "high"})).content == "ok"


def test_unknown_metered_price_and_missing_approval_prevent_dispatch(tmp_path) -> None:
    store = ChatStore(tmp_path / "metered-policy.sqlite3")
    registry = ProviderRegistry(store)
    registry.add_provider("host", "Host", "https://host.example")
    registry.add_model("coder", "coder", "v1")
    registry.add_offering("paid", "host", "coder", "direct", "metered")
    registry.set_offering_state("paid", status="eligible", health_state="healthy", actor="test", reason="fixture")
    adapter = FakeAdapter()
    gateway = ControlledGateway(InferencePolicy(registry), adapter)

    with pytest.raises(PermissionError) as exc:
        gateway.invoke(_request(offering_id="paid"))

    assert "price is unknown" in str(exc.value)
    assert "approval" in str(exc.value)
    assert "budget reservation" in str(exc.value)
    assert adapter.calls == 0


def test_metered_route_requires_real_request_bound_reservation(tmp_path) -> None:
    store = ChatStore(tmp_path / "metered-policy.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    registry = ProviderRegistry(store)
    registry.add_provider("host", "Host", "https://host.example")
    registry.add_model("coder", "coder", "v1")
    registry.add_offering("paid", "host", "coder", "direct", "metered")
    now = datetime.now(timezone.utc).isoformat()
    import_catalog_record(store, CatalogRecord(
        "paid", "https://host.example/prices", now, "https://host.example/terms",
        {"stream": True, "context_window": 4096, "max_output_tokens": 256},
        "private", True, 1.0, 2.0, {},
    ))
    registry.set_offering_state("paid", status="eligible", health_state="healthy", actor="test", reason="fixture")
    store.record_action_authorization(
        project_id=project.id, action="inference_route", scope="paid", risk="financial",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    budget = BudgetManager(store)
    budget.set_project_limit(project.id, 0.01, actor="user")
    reservation = budget.reserve("request-1", project.id, 0.001)
    request = _request(offering_id="paid")
    request = InferenceRequest(
        request.request_id, project.id, request.offering_id, request.model_id,
        request.purpose, request.messages, request.sensitivity,
        request.required_capabilities, request.max_input_tokens,
        request.max_output_tokens, request.timeout_seconds,
        reservation_id=reservation, stream=True,
    )
    adapter = FakeAdapter()
    gateway = ControlledGateway(InferencePolicy(registry), adapter, InferenceLedger(store))

    assert gateway.invoke(request).status == "completed"
    assert adapter.calls == 1
    events = InferenceLedger(store).events(request.request_id)
    assert events[0]["cost_accuracy"] == "estimated"
    assert events[0]["estimated_cost_usd"] == pytest.approx(0.00014)
    assert events[1]["actual_cost_usd"] is None
    assert budget.active_amount(reservation, "request-1", project.id) is None
    with pytest.raises(PermissionError, match="reservation"):
        gateway.invoke(replace(request, request_id="request-2"))
    assert adapter.calls == 1
