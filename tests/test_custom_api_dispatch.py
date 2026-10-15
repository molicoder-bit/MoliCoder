import io
import json
import urllib.error
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from local_model_chat.inference.adapters.openai_compatible import OpenAICompatibleAdapter
from local_model_chat.inference.api_dispatch import prepare_custom_chat, preview_custom_chat
from local_model_chat.inference import eligibility
from local_model_chat.environment.security import platform_security
from local_model_chat.inference.budget import BudgetManager
from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.inference.contracts import InferenceEventKind, InferencePurpose, InferenceRequest
from local_model_chat.inference.ledger import InferenceLedger
from local_model_chat.inference.onboarding import (
    CustomRoute, CustomRouteFacts, attest_custom_route_facts,
    check_custom_connection, register_custom_route, register_preset_route,
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
        "private", 8192, 512, 1.0, 2.0, True, True,
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
    stream_request = replace(prepare_custom_chat(registry, project.id, offering_id, "Hi"), stream=True)
    stream_adapter = OpenAICompatibleAdapter(
        registry, Credentials(),
        opener=lambda *_args, **_kwargs: io.BytesIO(
            b'data: {"choices":[{"delta":{"content":"Part"}}]}\n\n'
            b'data: [DONE]\n\n'
        ),
    )
    stream_gateway = ControlledGateway(
        InferencePolicy(registry), stream_adapter, InferenceLedger(store),
    )
    stream = stream_gateway.stream(stream_request)
    assert next(stream).kind == InferenceEventKind.STARTED
    assert next(stream).kind == InferenceEventKind.CONTENT_DELTA
    stream_adapter.cancel(stream_request.request_id)
    assert list(stream)[-1].kind == InferenceEventKind.CANCELLED
    with store._connect() as connection:
        row = connection.execute(
            "SELECT status, amount_usd, actual_usd FROM budget_reservations WHERE id = ?",
            (stream_request.reservation_id,),
        ).fetchone()
    assert row[0] == "settled" and row[2] == row[1]
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


def test_custom_stream_tracks_usage_and_can_be_cancelled(tmp_path) -> None:
    store = ChatStore(tmp_path / "stream.sqlite3")
    registry = ProviderRegistry(store)
    offering_id = register_custom_route(store, CustomRoute(
        "local-api", "Local API", "http://127.0.0.1:12345/v1", "model-a",
    ))
    attest_custom_route_facts(store, CustomRouteFacts(
        offering_id, "https://example.test/model", "", "private", 8192, 512,
        None, None, True, True,
    ))
    registry.set_offering_state(
        offering_id, status="eligible", health_state="healthy", actor="user", reason="fixture",
    )
    request = InferenceRequest(
        "stream-1", None, offering_id, "model-a", InferencePurpose.CHAT,
        ({"role": "user", "content": "Hi"},), "private", (), 100, 20, 5,
        stream=True,
    )
    body = (b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":1}}\n\n'
            b'data: [DONE]\n\n')
    gateway = ControlledGateway(
        InferencePolicy(registry),
        OpenAICompatibleAdapter(registry, opener=lambda *_args, **_kwargs: io.BytesIO(body)),
        InferenceLedger(store),
    )
    events = list(gateway.stream(request))
    assert [item.kind for item in events] == [
        InferenceEventKind.STARTED, InferenceEventKind.CONTENT_DELTA,
        InferenceEventKind.COMPLETED,
    ]
    assert events[1].payload["text"] == "Hello"
    assert events[-1].payload["metrics"].prompt_tokens == 4
    assert InferenceLedger(store).events(request.request_id)[-1]["input_tokens"] == 4

    cancelled = InferenceRequest(
        "stream-2", None, offering_id, "model-a", InferencePurpose.CHAT,
        request.messages, "private", (), 100, 20, 5, stream=True,
    )
    adapter = OpenAICompatibleAdapter(registry, opener=lambda *_args, **_kwargs: io.BytesIO(body))
    stream = adapter.stream(cancelled)
    assert next(stream).kind == InferenceEventKind.STARTED
    assert next(stream).kind == InferenceEventKind.CONTENT_DELTA
    adapter.cancel(cancelled.request_id)
    assert list(stream)[-1].kind == InferenceEventKind.CANCELLED


def test_custom_stream_redirect_and_malformed_payload_fail_closed(tmp_path) -> None:
    store = ChatStore(tmp_path / "stream-failure.sqlite3")
    registry = ProviderRegistry(store)
    offering_id = register_custom_route(store, CustomRoute(
        "local-api", "Local API", "http://127.0.0.1:12345/v1", "model-a",
    ))
    request = InferenceRequest(
        "stream-1", None, offering_id, "model-a", InferencePurpose.CHAT,
        ({"role": "user", "content": "Hi"},), "public", (), 100, 20, 5, stream=True,
    )

    def redirect(transport_request, *, timeout):
        raise urllib.error.HTTPError(transport_request.full_url, 302, "redirect", {}, None)

    adapter = OpenAICompatibleAdapter(registry, opener=redirect)
    assert list(adapter.stream(request))[-1].kind == InferenceEventKind.FAILED
    malformed = OpenAICompatibleAdapter(
        registry, opener=lambda *_args, **_kwargs: io.BytesIO(b"data: {bad}\n\n"),
    )
    assert list(malformed.stream(request))[-1].kind == InferenceEventKind.FAILED


def test_real_loopback_redirect_is_never_followed(tmp_path) -> None:
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/sink")
            self.end_headers()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = ChatStore(tmp_path / "redirect.sqlite3")
        registry = ProviderRegistry(store)
        offering_id = register_custom_route(store, CustomRoute(
            "local-api", "Local API", f"http://127.0.0.1:{server.server_port}/v1", "model-a",
        ))
        request = InferenceRequest(
            "redirect-1", None, offering_id, "model-a", InferencePurpose.CHAT,
            ({"role": "user", "content": "Hi"},), "public", (), 100, 20, 5,
        )
        result = OpenAICompatibleAdapter(registry).invoke(request)
        assert result.status == "failed" and result.error.value == "unavailable"
        assert seen == ["/v1/chat/completions"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_custom_chat_classifies_auth_and_timeout_without_server_body(tmp_path) -> None:
    store = ChatStore(tmp_path / "errors.sqlite3")
    registry = ProviderRegistry(store)
    offering_id = register_custom_route(store, CustomRoute(
        "local-api", "Local API", "http://127.0.0.1:12345/v1", "model-a",
    ))
    request = InferenceRequest(
        "failure-1", None, offering_id, "model-a", InferencePurpose.CHAT,
        ({"role": "user", "content": "Hi"},), "public", (), 100, 20, 5,
    )

    def reject(transport_request, *, timeout):
        raise urllib.error.HTTPError(transport_request.full_url, 401, "private provider body", {}, None)

    unauthorized = OpenAICompatibleAdapter(registry, opener=reject).invoke(request)
    assert unauthorized.error.value == "auth" and unauthorized.content == ""

    def timeout(_request, *, timeout):
        raise TimeoutError("private provider body")

    expired = OpenAICompatibleAdapter(registry, opener=timeout).invoke(request)
    assert expired.error.value == "timeout" and expired.content == ""


def test_official_preset_uses_the_same_budget_and_policy_gateway(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(eligibility, "platform_security", lambda: platform_security("Windows"))
    store = ChatStore(tmp_path / "preset.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    registry = ProviderRegistry(store)
    offering_id = register_preset_route(store, "openai-api", "example-model")
    attest_custom_route_facts(store, CustomRouteFacts(
        offering_id, "https://platform.openai.com/pricing",
        "https://openai.com/policies/service-terms/", "private", 8192, 512,
        1.0, 2.0, True,
    ))
    account_id = registry.get_offering(offering_id).account_id
    registry.update_account_secret(account_id, "openai-api", "encrypted-local:" + "a" * 64)

    class Credentials:
        def get(self, _reference):
            return "private-token"

    check_custom_connection(
        store, offering_id, credentials=Credentials(),
        opener=lambda *_args, **_kwargs: Response({"data": [{"id": "example-model"}]}),
    )
    registry.set_offering_state(
        offering_id, status="eligible", actor="user", reason="reviewed fixture",
    )
    BudgetManager(store).set_project_limit(project.id, 0.01, actor="user")
    store.record_action_authorization(
        project_id=project.id, action="inference_route", scope=offering_id,
        risk="financial", decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    urls = []

    def open_request(transport_request, *, timeout):
        urls.append(transport_request.full_url)
        return Response({"choices": [{"message": {"content": "ok"}}]})

    request = prepare_custom_chat(registry, project.id, offering_id, "Hi")
    result = ControlledGateway(
        InferencePolicy(registry),
        OpenAICompatibleAdapter(registry, Credentials(), opener=open_request),
        InferenceLedger(store),
    ).invoke(request)
    assert result.content == "ok"
    assert urls == ["https://api.openai.com/v1/chat/completions"]
