"""Bounded transport for an explicitly registered OpenAI-compatible chat route."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterator

from ...environment.auth import ProtectedCredentialStore
from ..contracts import InferenceError, InferenceEvent, InferenceEventKind, InferenceRequest, InferenceResult
from ..onboarding import _NoRedirects, is_api_offering, validate_custom_endpoint
from ..registry import ProviderRegistry


class OpenAICompatibleAdapter:
    """Transport only. ControlledGateway must authorize before invoking it."""

    def __init__(self, registry: ProviderRegistry, credentials: ProtectedCredentialStore | None = None, *, opener=None) -> None:
        self.registry = registry
        self.credentials = credentials
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirects,
        ).open
        self._cancelled: set[str] = set()
        self._active_responses: dict[str, object] = {}
        self._lock = threading.Lock()

    def probe_capabilities(self) -> dict[str, bool]:
        return {"chat": True, "stream": True}

    def cancel(self, request_id: str) -> None:
        with self._lock:
            self._cancelled.add(request_id)
            response = self._active_responses.get(request_id)
        if response is not None:
            response.close()

    def _is_cancelled(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._cancelled

    def invoke(self, request: InferenceRequest) -> InferenceResult:
        offering = self.registry.get_offering(request.offering_id)
        if offering is None or not is_api_offering(offering.id) or offering.model_id != request.model_id:
            raise ValueError("registered custom offering and model are required")
        endpoint = validate_custom_endpoint(offering.route_id)
        if request.stream or request.tool_scopes:
            raise ValueError("this transport supports non-streaming chat without tools")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if offering.account_id:
            if self.credentials is None:
                raise PermissionError("protected credential store is required")
            with self.registry.store._connect() as connection:
                row = connection.execute(
                    "SELECT secret_ref FROM inference_accounts WHERE id = ? AND provider_id = ?",
                    (offering.account_id, offering.provider_id),
                ).fetchone()
            if row is None or not row[0]:
                raise PermissionError("provider API key is missing")
            headers["Authorization"] = "Bearer " + self.credentials.get(str(row[0]))
        if self._is_cancelled(request.request_id):
            return self._result(request, "cancelled", error=InferenceError.CANCELLED)
        body = {
            "model": request.model_id,
            "messages": request.messages,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.options.get('reasoning_effort') is not None:
            if offering.capabilities.get('reasoning_effort_parameter') != 'reasoning_effort':
                raise ValueError('Reasoning effort transport is not configured for this offering')
            body['reasoning_effort'] = request.options['reasoning_effort']
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        if len(payload) > 1_048_576:
            raise ValueError("request body exceeds transport limit")
        transport_request = urllib.request.Request(
            endpoint + "/chat/completions", data=payload, headers=headers, method="POST",
        )
        started = time.monotonic()
        try:
            with self.opener(transport_request, timeout=min(request.timeout_seconds, 120)) as response:
                raw = response.read(4_194_305)
                provider_request_id = response.headers.get("x-request-id", "")[:200]
        except urllib.error.HTTPError as exc:
            error = (InferenceError.AUTH if exc.code in {401, 403} else
                     InferenceError.RATE_LIMIT if exc.code == 429 else
                     InferenceError.UNAVAILABLE)
            return self._result(request, "failed", error=error, elapsed=started)
        except TimeoutError:
            return self._result(request, "failed", error=InferenceError.TIMEOUT, elapsed=started)
        except (OSError, urllib.error.URLError):
            return self._result(request, "failed", error=InferenceError.UNAVAILABLE, elapsed=started)
        if self._is_cancelled(request.request_id):
            return self._result(request, "cancelled", error=InferenceError.CANCELLED, elapsed=started)
        if len(raw) > 4_194_304:
            return self._result(request, "failed", error=InferenceError.INVALID_RESPONSE, elapsed=started)
        try:
            data = json.loads(raw)
            choice = data["choices"][0]["message"]
            content = choice["content"]
            if not isinstance(content, str):
                raise ValueError("content is not text")
            usage = data.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
        except (UnicodeError, ValueError, KeyError, IndexError, TypeError):
            return self._result(request, "failed", error=InferenceError.INVALID_RESPONSE, elapsed=started)
        return InferenceResult(
            request.request_id, "completed", offering.provider_id, request.model_id,
            content, input_tokens=_token_count(usage.get("prompt_tokens")),
            output_tokens=_token_count(usage.get("completion_tokens")),
            provider_request_id=provider_request_id, total_seconds=time.monotonic() - started,
        )

    def stream(self, request: InferenceRequest) -> Iterator[InferenceEvent]:
        offering = self.registry.get_offering(request.offering_id)
        if offering is None or not is_api_offering(offering.id) or offering.model_id != request.model_id:
            raise ValueError("registered custom offering and model are required")
        endpoint = validate_custom_endpoint(offering.route_id)
        if not request.stream or request.tool_scopes:
            raise ValueError("streaming chat requires stream mode without tools")
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if offering.account_id:
            if self.credentials is None:
                raise PermissionError("protected credential store is required")
            with self.registry.store._connect() as connection:
                row = connection.execute(
                    "SELECT secret_ref FROM inference_accounts WHERE id = ? AND provider_id = ?",
                    (offering.account_id, offering.provider_id),
                ).fetchone()
            if row is None or not row[0]:
                raise PermissionError("provider API key is missing")
            headers["Authorization"] = "Bearer " + self.credentials.get(str(row[0]))
        payload = json.dumps({
            "model": request.model_id, "messages": request.messages,
            "max_tokens": request.max_output_tokens, "stream": True,
        }, separators=(",", ":")).encode("utf-8")
        if len(payload) > 1_048_576:
            raise ValueError("request body exceeds transport limit")
        sequence = 0
        now = lambda: datetime.now(timezone.utc).isoformat(timespec="microseconds")
        if self._is_cancelled(request.request_id):
            yield InferenceEvent(request.request_id, sequence, InferenceEventKind.CANCELLED, now())
            return
        transport_request = urllib.request.Request(
            endpoint + "/chat/completions", data=payload, headers=headers, method="POST",
        )
        try:
            response = self.opener(transport_request, timeout=min(request.timeout_seconds, 120))
        except urllib.error.HTTPError:
            yield InferenceEvent(request.request_id, sequence, InferenceEventKind.FAILED, now())
            return
        except (OSError, urllib.error.URLError, TimeoutError):
            yield InferenceEvent(request.request_id, sequence, InferenceEventKind.FAILED, now())
            return
        with self._lock:
            self._active_responses[request.request_id] = response
        total_bytes = 0
        done = False
        usage: dict = {}
        try:
            yield InferenceEvent(request.request_id, sequence, InferenceEventKind.STARTED, now())
            with response:
                for raw in response:
                    if self._is_cancelled(request.request_id):
                        break
                    total_bytes += len(raw)
                    if total_bytes > 4_194_304 or len(raw) > 262_144:
                        raise ValueError("provider stream exceeded its size limit")
                    if not raw.startswith(b"data:"):
                        continue
                    data = raw[5:].strip()
                    if data == b"[DONE]":
                        done = True
                        break
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise ValueError("invalid stream event")
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    choices = event.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            sequence += 1
                            yield InferenceEvent(
                                request.request_id, sequence, InferenceEventKind.CONTENT_DELTA,
                                now(), {"text": content},
                            )
            sequence += 1
            if self._is_cancelled(request.request_id):
                yield InferenceEvent(request.request_id, sequence, InferenceEventKind.CANCELLED, now())
            elif done:
                metrics = SimpleNamespace(
                    prompt_tokens=_token_count(usage.get("prompt_tokens")),
                    output_tokens=_token_count(usage.get("completion_tokens")),
                )
                yield InferenceEvent(
                    request.request_id, sequence, InferenceEventKind.COMPLETED, now(),
                    {"metrics": metrics},
                )
            else:
                yield InferenceEvent(request.request_id, sequence, InferenceEventKind.FAILED, now())
        except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
            sequence += 1
            kind = (InferenceEventKind.CANCELLED if self._is_cancelled(request.request_id)
                    else InferenceEventKind.FAILED)
            yield InferenceEvent(request.request_id, sequence, kind, now())
        finally:
            with self._lock:
                self._active_responses.pop(request.request_id, None)

    def _result(
        self, request: InferenceRequest, status: str, *, error: InferenceError,
        elapsed: float | None = None,
    ) -> InferenceResult:
        offering = self.registry.get_offering(request.offering_id)
        return InferenceResult(
            request.request_id, status, offering.provider_id if offering else "unknown",
            request.model_id, "", error=error,
            total_seconds=time.monotonic() - elapsed if elapsed is not None else 0.0,
        )


def _token_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
