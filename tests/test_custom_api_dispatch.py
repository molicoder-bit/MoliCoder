import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.inference.adapters.openai_compatible import OpenAICompatibleAdapter
from local_model_chat.inference.api_dispatch import prepare_custom_chat, preview_custom_chat
from local_model_chat.inference import eligibility
from local_model_chat.environment.security import platform_security
from local_model_chat.inference.budget import BudgetManager
from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
from local_model_chat.inference.ledger import InferenceLedger
from local_model_chat.inference.onboarding import (
    CustomRoute, CustomRouteFacts, attest_custom_route_facts,
    check_custom_connection, register_custom_route,
)
from local_model_chat.inference.policy import ControlledGateway, InferencePolicy
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())
        self.headers = {"x-request-id": "provider-123"}


def test_custom_local_api_goes_through_policy_and_records_usage(tmp_path) -> None:
    store = ChatStore(tmp_path / "api.sqlite3")
    registry = ProviderRegistry(store)
    offering_id = register_custom_route(store, CustomRoute(
        "local-api", "Local API", "http://127.0.0.1:12345/v1", "model-a",
    ))
    calls = []

    def open_request(request, *, timeout):
        calls.append((request.full_url, request.get_method(), timeout, json.loads(request.data)))
        return Response({
            "choices": [{"message": {"content": "Answer"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        })

    gateway = ControlledGateway(
        InferencePolicy(registry), OpenAICompatibleAdapter(registry, opener=open_request),
        InferenceLedger(store),
    )
    request = InferenceRequest(
        "request-1", None, offering_id, "model-a", InferencePurpose.CHAT,
        ({"role": "user", "content": "Hi"},), "private", (), 100, 20, 5,
    )
    with pytest.raises(PermissionError, match="discovered"):
        gateway.invoke(request)
    assert calls == []

    now = datetime.now(timezone.utc).isoformat()
    import_catalog_record(store, CatalogRecord(
        offering_id, "https://example.test/source", now, "",
        {"context_window": 4096, "max_output_tokens": 256},
        "public", True, None, None, {},
    ))
    registry.set_offering_state(
        offering_id, status="eligible", health_state="healthy", actor="user", reason="local test",
    )
    # Privacy also blocks transport before it sees the request body.
    request = InferenceRequest(
        "request-2", None, offering_id, "model-a", InferencePurpose.CHAT,
        ({"role": "user", "content": "Hi"},), "private", (), 100, 20, 5,
    )
    with pytest.raises(PermissionError, match="privacy"):
        gateway.invoke(request)
    assert calls == []
    import_catalog_record(store, CatalogRecord(
        offering_id, "https://example.test/source", now, "",
        {"context_window": 4096, "max_output_tokens": 256},
        "restricted", True, None, None, {},
    ))
    # A denied request ID is immutable in the append-only ledger.
    request = InferenceRequest(
        "request-3", None, offering_id, "model-a", InferencePurpose.CHAT,
        ({"role": "user", "content": "Hi"},), "private", (), 100, 20, 5,
    )
    result = gateway.invoke(request)
    assert result.status == "completed" and result.content == "Answer"
    assert result.input_tokens == 7 and result.output_tokens == 2
    assert result.provider_request_id == "provider-123"
    assert calls == [(
        "http://127.0.0.1:12345/v1/chat/completions", "POST", 5,
        {"model": "model-a", "messages": [{"role": "user", "content": "Hi"}],
         "max_tokens": 20, "stream": False},
    )]
    assert [event["status"] for event in InferenceLedger(store).events("request-3")] == [
        "started", "completed",
    ]


def test_custom_api_cancellation_and_invalid_response(tmp_path) -> None:
    store = ChatStore(tmp_path / "api.sqlite3")
    registry = ProviderRegistry(store)
    offering_id = register_custom_route(store, CustomRoute(
        "local-api", "Local API", "http://127.0.0.1:12345/v1", "model-a",
    ))
    request = InferenceRequest(
        "request-1", None, offering_id, "model-a", InferencePurpose.CHAT,
        ({"role": "user", "content": "Hi"},), "public", (), 10, 10, 5,
    )
    adapter = OpenAICompatibleAdapter(registry, opener=lambda *_args, **_kwargs: Response({"bad": True}))
    adapter.cancel(request.request_id)
    assert adapter.invoke(request).status == "cancelled"
    second = InferenceRequest(
        "request-2", None, offering_id, "model-a", InferencePurpose.CHAT,
        request.messages, "public", (), 10, 10, 5,
    )
    assert adapter.invoke(second).error.value == "invalid_response"


def test_paid_custom_api_requires_budget_and_exact_approval_before_dispatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(eligibility, "platform_security", lambda: platform_security("Windows"))
    store = ChatStore(tmp_path / "paid-api.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    registry = ProviderRegistry(store)
    offering_id = register_custom_route(store, CustomRoute(
        "remote-api", "Remote API", "https://api.example.test/v1", "model-a",
    ))
    attest_custom_route_facts(store, CustomRouteFacts(
        offering_id, "https://api.example.test/pricing", "https://api.example.test/terms",
        "private", 8192, 512, 1.0, 2.0, True,
    ))
    registry.update_account_secret(
        "manual-key:remote-api", "remote-api", "encrypted-local:" + "a" * 64,
    )

    class Credentials:
        def get(self, _reference):
            return "private-token"

    check_custom_connection(
        store, offering_id, credentials=Credentials(),
        opener=lambda *_args, **_kwargs: Response({"data": [{"id": "model-a"}]}),
    )
    registry.set_offering_state(
        offering_id, status="eligible", actor="user", reason="verified fixture",
    )
    with pytest.raises(PermissionError, match="hard budget"):
        prepare_custom_chat(registry, project.id, offering_id, "Hi")
    budget = BudgetManager(store)
    budget.set_project_limit(project.id, 0.01, actor="user")
    preview = preview_custom_chat(registry, offering_id, "Hi")
    assert 0 < preview.worst_case_usd < 0.01
    request = prepare_custom_chat(registry, project.id, offering_id, "Hi")
    calls = []

    def open_request(transport_request, *, timeout):
        calls.append(transport_request.full_url)
        assert transport_request.get_header("Authorization") == "Bearer private-token"
        return Response({
            "choices": [{"message": {"content": "Answer"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        })

    gateway = ControlledGateway(
        InferencePolicy(registry),
        OpenAICompatibleAdapter(registry, Credentials(), opener=open_request),
        InferenceLedger(store),
    )
    with pytest.raises(PermissionError, match="approval"):
        gateway.invoke(request)
    assert calls == []
    store.record_action_authorization(
        project_id=project.id, action="inference_route", scope=offering_id,
        risk="financial", decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    request = prepare_custom_chat(registry, project.id, offering_id, "Hi")
    result = gateway.invoke(request)
    assert result.status == "completed" and result.content == "Answer"
    assert calls == ["https://api.example.test/v1/chat/completions"]
    assert [event["status"] for event in InferenceLedger(store).events(request.request_id)] == [
        "started", "completed",
    ]
    with store._connect() as connection:
        connection.execute(
            "UPDATE inference_offerings SET price_checked_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", offering_id),
        )
        connection.commit()
    stale_request = prepare_custom_chat(registry, project.id, offering_id, "Hi")
    with pytest.raises(PermissionError, match="stale"):
        gateway.invoke(stale_request)
    assert calls == ["https://api.example.test/v1/chat/completions"]
