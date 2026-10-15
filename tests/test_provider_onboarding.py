import io
import json
import urllib.error

import pytest

from local_model_chat.inference.onboarding import (
    CustomRoute, check_custom_connection, register_custom_route,
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
