import io
import json
import urllib.error

import pytest

from local_model_chat.environment.auth import store_provider_key
from local_model_chat.inference.onboarding import (
    CustomRoute, CustomRouteFacts, attest_custom_route_facts,
    check_custom_connection, register_custom_route, register_preset_route,
)
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


def test_custom_https_route_stays_inactive_without_key_or_price(tmp_path) -> None:
    store = ChatStore(tmp_path / "catalog.sqlite3")
    offering_id = register_custom_route(store, CustomRoute(
        "example-api", "Example API", "https://api.example.test/v1/", "sample/model-1",
    ))
    registry = ProviderRegistry(store)
    offering = registry.get_offering(offering_id)
    assert offering.route_id == "https://api.example.test/v1"
    assert offering.status == "discovered" and offering.offering_class == "metered"
    assert offering.input_usd_per_million is None
    assert registry.list_account_statuses()[0].auth_status == "login_needed"
    assert not registry.list_account_statuses()[0].credential_present
    with pytest.raises(ValueError, match="already registered"):
        register_custom_route(store, CustomRoute(
            "example-api", "Other", "https://other.example.test/v1", "model-2",
        ))
    assert len(registry.list_offerings()) == 1


def test_custom_loopback_route_is_keyless_and_unsafe_urls_do_not_mutate(tmp_path) -> None:
    store = ChatStore(tmp_path / "catalog.sqlite3")
    registry = ProviderRegistry(store)
    for endpoint in (
        "http://example.test/v1", "https://user:secret@example.test/v1",
        "https://api.example.test/v1?token=secret", "https://api.example.test/v1#fragment",
        "http://127.0.0.1:bad/v1", "https://api.example.test/v1/chat/completions",
    ):
        with pytest.raises(ValueError):
            register_custom_route(store, CustomRoute("bad-route", "Bad", endpoint, "model"))
    assert registry.list_providers() == ()
    offering_id = register_custom_route(store, CustomRoute(
        "my-local", "My local service", "http://127.0.0.1:12345/v1", "model",
    ))
    offering = registry.get_offering(offering_id)
    assert offering.offering_class == "local" and offering.account_id is None
    assert offering.status == "discovered"


def test_connection_check_is_get_only_and_keeps_route_inactive(tmp_path) -> None:
    store = ChatStore(tmp_path / "catalog.sqlite3")
    offering_id = register_custom_route(store, CustomRoute(
        "local-api", "Local API", "http://127.0.0.1:12345/v1", "model-a",
    ))
    calls = []

    def open_request(request, *, timeout):
        calls.append((request.full_url, request.get_method(), timeout))
        return io.BytesIO(json.dumps({"data": [{"id": "model-a"}]}).encode())

    result = check_custom_connection(store, offering_id, opener=open_request)
    offering = ProviderRegistry(store).get_offering(offering_id)
    assert result.status == "ready"
    assert calls == [("http://127.0.0.1:12345/v1/models", "GET", 5)]
    assert offering.health_state == "healthy" and offering.status == "discovered"


def test_connection_check_needs_key_and_classifies_auth_without_leaking_body(tmp_path) -> None:
    store = ChatStore(tmp_path / "catalog.sqlite3")
    offering_id = register_custom_route(store, CustomRoute(
        "remote-api", "Remote API", "https://api.example.test/v1", "model-a",
    ))
    assert check_custom_connection(store, offering_id).status == "key_needed"
    registry = ProviderRegistry(store)
    registry.update_account_secret(
        "manual-key:remote-api", "remote-api", "encrypted-local:" + "a" * 64,
    )

    class Credentials:
        def get(self, reference):
            assert reference.startswith("encrypted-local:")
            return "private-token"

    def reject(request, *, timeout):
        assert request.get_header("Authorization") == "Bearer private-token"
        raise urllib.error.HTTPError(request.full_url, 401, "private details", {}, None)

    result = check_custom_connection(
        store, offering_id, credentials=Credentials(), opener=reject,
    )
    assert result.status == "auth_required"
    assert "private details" not in result.detail
    assert registry.get_account_status("manual-key:remote-api") == "login_needed"
    assert registry.get_offering(offering_id).status == "discovered"


def test_connection_check_rejects_tampered_endpoint_before_network(tmp_path) -> None:
    store = ChatStore(tmp_path / "catalog.sqlite3")
    offering_id = register_custom_route(store, CustomRoute(
        "local-api", "Local API", "http://127.0.0.1:12345/v1", "model-a",
    ))
    with store._connect() as connection:
        connection.execute(
            "UPDATE inference_offerings SET route_id = ? WHERE id = ?",
            ("http://remote.example.test/v1", offering_id),
        )
        connection.commit()
    with pytest.raises(ValueError, match="unsafe"):
        check_custom_connection(store, offering_id, opener=lambda *_args, **_kwargs: pytest.fail("network called"))


def test_user_attested_paid_facts_keep_route_inactive_and_preserve_source(tmp_path) -> None:
    store = ChatStore(tmp_path / "catalog.sqlite3")
    offering_id = register_custom_route(store, CustomRoute(
        "remote-api", "Remote API", "https://api.example.test/v1", "model-a",
    ))
    facts = CustomRouteFacts(
        offering_id, "https://api.example.test/pricing", "https://api.example.test/terms",
        "public", 8192, 1024, 1.5, 6.0, True,
    )
    attest_custom_route_facts(store, facts)
    registry = ProviderRegistry(store)
    offering = registry.get_offering(offering_id)
    assert offering.status == "discovered"
    assert offering.input_usd_per_million == 1.5
    assert offering.output_usd_per_million == 6.0
    assert offering.capabilities["context_window"] == 8192
    assert offering.terms_checked_at and offering.price_checked_at
    assert registry.list_events(offering_id)[-1]["actor"] == "user"
    with pytest.raises(ValueError, match="HTTPS"):
        attest_custom_route_facts(store, CustomRouteFacts(
            offering_id, "http://api.example.test/pricing", facts.terms_url,
            "public", 8192, 1024, 1.5, 6.0, True,
        ))
    assert registry.get_offering(offering_id).source_url == facts.source_url


def test_preset_models_share_one_provider_account_and_stay_inactive(tmp_path) -> None:
    store = ChatStore(tmp_path / "presets.sqlite3")
    first = register_preset_route(store, "openai-api", "example-model-a")
    second = register_preset_route(store, "openai-api", "example-model-b")
    router = register_preset_route(store, "openrouter-api", "vendor/example-model")
    registry = ProviderRegistry(store)
    assert len({registry.get_offering(item).account_id for item in (first, second)}) == 2
    assert registry.get_offering(first).route_id == "https://api.openai.com/v1"
    assert registry.get_offering(router).route_id == "https://openrouter.ai/api/v1"
    assert all(registry.get_offering(item).status == "discovered"
               for item in (first, second, router))
    assert all(registry.get_offering(item).input_usd_per_million is None
               for item in (first, second, router))
    class Credentials:
        def put(self, _account_id, _secret):
            return "encrypted-local:" + "a" * 64

    store_provider_key(registry, Credentials(), "openai-api", "private-key")
    with store._connect() as connection:
        references = connection.execute(
            "SELECT DISTINCT secret_ref FROM inference_accounts WHERE provider_id = 'openai-api'"
        ).fetchall()
    assert [row[0] for row in references] == ["encrypted-local:" + "a" * 64]
    third = register_preset_route(store, "openai-api", "example-model-c")
    with store._connect() as connection:
        account = connection.execute(
            "SELECT secret_ref FROM inference_accounts WHERE id = ?",
            (registry.get_offering(third).account_id,),
        ).fetchone()
    assert account[0] == "encrypted-local:" + "a" * 64
    with pytest.raises(ValueError, match="already registered"):
        register_preset_route(store, "openai-api", "example-model-a")
    with pytest.raises(ValueError, match="reserved"):
        register_custom_route(store, CustomRoute(
            "openai-api", "Wrong", "https://other.example/v1", "model",
        ))
