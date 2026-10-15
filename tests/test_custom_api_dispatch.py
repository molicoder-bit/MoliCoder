import io
import json
from datetime import datetime, timezone

import pytest

from local_model_chat.inference.adapters.openai_compatible import OpenAICompatibleAdapter
from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
from local_model_chat.inference.ledger import InferenceLedger
from local_model_chat.inference.onboarding import CustomRoute, register_custom_route
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
