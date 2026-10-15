"""Persist user-supplied OpenAI-compatible routes without activating them."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..environment.auth import CredentialUnavailable, ProtectedCredentialStore
from ..storage import ChatStore
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


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


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
    endpoint = route.base_url.rstrip("/")
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
    local = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
    if parsed.scheme == "http" and not local:
        raise ValueError("plain HTTP is allowed only for a loopback endpoint")
    if parsed.path.endswith(("/chat/completions", "/responses")):
        raise ValueError("enter the API base URL, not a request endpoint")
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
    endpoint = offering.route_id.rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(character.isspace() for character in endpoint)
            or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1"})):
        raise ValueError("custom endpoint is unsafe")
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
