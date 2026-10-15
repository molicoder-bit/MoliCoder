"""Persist user-supplied OpenAI-compatible routes without activating them."""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..environment.auth import CredentialUnavailable, ProtectedCredentialStore
from ..storage import ChatStore
from .catalog import CatalogRecord, import_catalog_record
from .registry import ProviderRegistry


@dataclass(frozen=True, slots=True)
class CustomRoute:
    provider_id: str
    name: str
    base_url: str
    model_id: str


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class CustomRouteFacts:
    offering_id: str
    source_url: str
    terms_url: str
    privacy_class: str
    context_window: int
    max_output_tokens: int
    input_usd_per_million: float | None
    output_usd_per_million: float | None
    automation_allowed: bool
    supports_stream: bool = False


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def validate_custom_endpoint(base_url: str) -> str:
    """Return an exact API base URL suitable for both checks and dispatch."""
    endpoint = base_url.rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.netloc or any(character.isspace() for character in endpoint)):
        raise ValueError("endpoint must be an exact HTTPS or loopback HTTP base URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint port is invalid")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError("plain HTTP is allowed only for a loopback endpoint")
    if parsed.path.endswith(("/chat/completions", "/responses")):
        raise ValueError("enter the API base URL, not a request endpoint")
    return endpoint


def register_custom_route(store: ChatStore, route: CustomRoute) -> str:
    """Register a disabled route; setup and provider verification follow later."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,39}", route.provider_id):
        raise ValueError("provider ID needs 3-40 lowercase letters, digits, or hyphens")
    if route.provider_id in {"ollama", "codex", "claude", "copilot"}:
        raise ValueError("built-in provider ID is reserved")
    if not route.name.strip() or len(route.name) > 100:
        raise ValueError("provider name is required and must be at most 100 characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,149}", route.model_id):
        raise ValueError("model ID contains unsupported characters")
    endpoint = validate_custom_endpoint(route.base_url)
    parsed = urlsplit(endpoint)
    local = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
    offering_id = "custom:" + route.provider_id + ":" + hashlib.sha256(
        (endpoint + "\n" + route.model_id).encode("utf-8")
    ).hexdigest()[:16]
    account_id = None if local else f"manual-key:{route.provider_id}"
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM inference_providers WHERE id = ?", (route.provider_id,),
        ).fetchone():
            raise ValueError("provider ID is already registered")
        connection.execute(
            """INSERT INTO inference_providers(id,name,official_url,created_at,updated_at)
               VALUES (?,?,?,?,?)""",
            (route.provider_id, route.name.strip(), endpoint, now, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO inference_models(id,family,version,created_at,updated_at)
               VALUES (?,?,?,?,?)""",
            (route.model_id, route.model_id, "user-supplied", now, now),
        )
        if account_id:
            connection.execute(
                """INSERT INTO inference_accounts(id,provider_id,owner,auth_status,created_at,updated_at)
                   VALUES (?,?, 'local_user', 'login_needed',?,?)""",
                (account_id, route.provider_id, now, now),
            )
        connection.execute(
            """INSERT INTO inference_offerings(
                   id,provider_id,model_id,route_id,account_id,class,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'discovered',?,?)""",
            (offering_id, route.provider_id, route.model_id, endpoint, account_id,
             "local" if local else "metered", now, now),
        )
        connection.execute(
            """INSERT INTO inference_offering_events(
                   offering_id,kind,snapshot_json,actor,created_at)
               VALUES (?,'custom_registration',?,'user',?)""",
            (offering_id, json.dumps({"base_url": endpoint, "status": "discovered"}), now),
        )
        connection.commit()
    return offering_id


def attest_custom_route_facts(store: ChatStore, facts: CustomRouteFacts) -> int:
    """Record user-reviewed facts, with provenance, without enabling the route."""
    offering = ProviderRegistry(store).get_offering(facts.offering_id)
    if offering is None or not offering.id.startswith("custom:"):
        raise ValueError("custom offering is unavailable")
    paid = offering.offering_class in {"metered", "trial"}
    if paid and not facts.terms_url:
        raise ValueError("paid routes need an official terms link")
    for url in (facts.source_url, facts.terms_url) if facts.terms_url else (facts.source_url,):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or any(ch.isspace() for ch in url)):
            raise ValueError("official price and terms links must use exact HTTPS URLs")
    if facts.privacy_class not in {"public", "private", "restricted"}:
        raise ValueError("select the documented data privacy class")
    if (not 1 <= facts.max_output_tokens <= facts.context_window <= 10_000_000):
        raise ValueError("model token limits are invalid")
    if paid and (facts.input_usd_per_million is None or facts.output_usd_per_million is None):
        raise ValueError("paid routes need both current token prices")
    for price in (facts.input_usd_per_million, facts.output_usd_per_million):
        if price is not None and (not math.isfinite(price) or price <= 0 or price > 1_000_000):
            raise ValueError("token prices must be finite and positive")
    checked_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return import_catalog_record(store, CatalogRecord(
        facts.offering_id, facts.source_url, checked_at, facts.terms_url,
        {"chat": True, "stream": facts.supports_stream, "context_window": facts.context_window,
         "max_output_tokens": facts.max_output_tokens},
        facts.privacy_class, facts.automation_allowed,
        facts.input_usd_per_million, facts.output_usd_per_million, {},
    ), actor="user", manual_override=True,
       reason="User reviewed the linked current price, terms, privacy and model limits.")


def check_custom_connection(
    store: ChatStore, offering_id: str, *,
    credentials: ProtectedCredentialStore | None = None,
    opener=None,
) -> ConnectionCheck:
    """Probe /models once without inference; never return a key or server body."""
    registry = ProviderRegistry(store)
    offering = registry.get_offering(offering_id)
    if offering is None or not offering.id.startswith("custom:"):
        raise ValueError("custom offering is unavailable")
    headers = {"Accept": "application/json"}
    if offering.account_id:
        with closing(store._connect()) as connection:
            row = connection.execute(
                "SELECT secret_ref FROM inference_accounts WHERE id = ?", (offering.account_id,),
            ).fetchone()
        if row is None or not row[0]:
            return ConnectionCheck("key_needed", "Store an API key in Provider settings first.")
        if credentials is None:
            raise ValueError("protected credential store is required")
        try:
            headers["Authorization"] = "Bearer " + credentials.get(str(row[0]))
        except (CredentialUnavailable, OSError, ValueError, UnicodeError):
            return ConnectionCheck("key_unavailable", "The stored API key cannot be read for this user.")
    # Recheck the persisted endpoint before making any network request.
    try:
        endpoint = validate_custom_endpoint(offering.route_id)
    except ValueError as exc:
        raise ValueError("custom endpoint is unsafe") from exc
    request = urllib.request.Request(
        endpoint + "/models", headers=headers, method="GET",
    )
    open_request = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirects,
    ).open
    status = "unavailable"
    detail = "Provider connection is unavailable."
    try:
        with open_request(request, timeout=5) as response:
            raw = response.read(262_145)
        if len(raw) > 262_144:
            status, detail = "invalid_response", "Provider model list exceeded the size limit."
        else:
            result = json.loads(raw)
            models = result.get("data") if isinstance(result, dict) else None
            if not isinstance(models, list):
                status, detail = "invalid_response", "Provider returned no compatible model list."
            elif any(isinstance(item, dict) and item.get("id") == offering.model_id for item in models):
                status, detail = "ready", "Endpoint, credential, and selected model are available."
            else:
                status, detail = "model_missing", "Selected model is absent from the provider model list."
    except urllib.error.HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            status, detail = "redirected", "Provider redirected the exact endpoint."
        elif exc.code in {401, 403}:
            status, detail = "auth_required", "Provider rejected the credential."
        else:
            status, detail = "http_error", f"Provider returned HTTP {exc.code}."
    except (OSError, urllib.error.URLError, TimeoutError):
        status, detail = "unavailable", "Provider connection is unavailable."
    except (UnicodeDecodeError, json.JSONDecodeError):
        status, detail = "invalid_response", "Provider returned invalid JSON."
    with closing(store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if offering.account_id:
            connection.execute(
                """UPDATE inference_accounts SET auth_status = ?, auth_checked_at = ?, updated_at = ?
                   WHERE id = ?""",
                ("ready" if status in {"ready", "model_missing"} else
                 "login_needed" if status == "auth_required" else "unknown",
                 datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
                 offering.account_id),
            )
        connection.commit()
    registry.set_offering_state(
        offering_id, health_state="healthy" if status in {"ready", "model_missing"} else "unavailable",
        actor="connection_check", reason=status,
    )
    return ConnectionCheck(status, detail)
