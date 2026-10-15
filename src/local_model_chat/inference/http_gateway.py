from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from uuid import uuid4

from ..context import estimate_messages, estimate_tokens
from ..storage import ChatStore
from .adapters.ollama import local_offering_id
from .contracts import InferencePurpose, InferenceRequest
from .ledger import InferenceLedger, UsageEvent
from .policy import InferencePolicy
from .registry import ProviderRegistry


class LocalPolicyHttpGateway:
    """Loopback OpenAI-compatible boundary for an approved local Ollama offering."""

    def __init__(
        self, store: ChatStore, project_id: int, run_id: int, model: str,
        *, context_window: int, max_output_tokens: int,
        upstream: str = "http://127.0.0.1:11434",
        opener: Callable = urllib.request.urlopen,
    ) -> None:
        if not model or model.endswith("-cloud"):
            raise ValueError("gateway requires a named local model")
        if context_window <= 0 or max_output_tokens <= 0:
            raise ValueError("gateway token limits must be positive")
        if upstream != "http://127.0.0.1:11434":
            raise ValueError("gateway upstream must be the local Ollama service")
        self.store = store
        self.project_id = project_id
        self.run_id = run_id
        self.model = model
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.upstream = upstream
        self.opener = opener
        self.token = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        if self._server is None:
            raise RuntimeError("gateway has not started")
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def start(self) -> None:
        if self._server is not None:
            return
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args) -> None:
                return

            def _json(self, status: int, value: dict) -> None:
                body = json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                return secrets.compare_digest(
                    self.headers.get("Authorization", ""), f"Bearer {gateway.token}",
                )

            def do_GET(self) -> None:
                if not self._authorized():
                    self._json(401, {"error": {"message": "gateway authentication required"}})
                elif self.path == "/v1/models":
                    self._json(200, {"object": "list", "data": [{"id": gateway.model, "object": "model"}]})
                else:
                    self._json(404, {"error": {"message": "unsupported endpoint"}})

            def do_POST(self) -> None:
                if not self._authorized():
                    self._json(401, {"error": {"message": "gateway authentication required"}})
                    return
                if self.path != "/v1/chat/completions":
                    self._json(404, {"error": {"message": "unsupported endpoint"}})
                    return
                request_id = ""
                ledger = None
                response_started = False
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4 * 1024 * 1024:
                        raise ValueError("request exceeds gateway size limit")
                    raw = self.rfile.read(length)
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or payload.get("model") != gateway.model:
                        raise ValueError("model is not the selected local offering")
                    messages = payload.get("messages")
                    if (not isinstance(messages, list) or not messages
                            or any(not isinstance(item, dict)
                                   or (not isinstance(item.get("content"), str)
                                       and not (item.get("content") is None and item.get("tool_calls")))
                                   for item in messages)):
                        raise ValueError("messages must contain text or a tool call")
                    if payload.get("stream", False) not in (True, False):
                        raise ValueError("stream must be a boolean")
                    output_limit = payload.get("max_completion_tokens", payload.get("max_tokens", gateway.max_output_tokens))
                    if not isinstance(output_limit, int) or not 0 < output_limit <= gateway.max_output_tokens:
                        raise ValueError("output token limit is invalid")
                    correlation = self.headers.get("X-MoliCoder-Request-ID", "")[:80]
                    request = InferenceRequest(
                        request_id=str(uuid4()), project_id=gateway.project_id,
                        offering_id=local_offering_id(gateway.model), model_id=gateway.model,
                        purpose=InferencePurpose.EXECUTION, messages=tuple(messages),
                        sensitivity="private", required_capabilities=(),
                        max_input_tokens=max(1, estimate_messages([
                            {"content": (item.get("content") or "") + json.dumps(item.get("tool_calls", []))}
                            for item in messages
                        ]) + estimate_tokens(json.dumps(payload.get("tools", [])))),
                        max_output_tokens=output_limit, timeout_seconds=1200,
                        stream=bool(payload.get("stream")),
                        tool_scopes=("agent-tools",) if payload.get("tools") else (),
                        context_packet_id=correlation or None, run_id=gateway.run_id,
                        options={"context_window": gateway.context_window},
                    )
                    ledger = InferenceLedger(gateway.store)
                    request_id = request.request_id
                    ledger.start(request)
                    decision = InferencePolicy(ProviderRegistry(gateway.store)).evaluate(request)
                    if not decision.allowed:
                        ledger.append(UsageEvent(request.request_id, "denied", source_event="policy:http_gateway"))
                        self._json(403, {"error": {"message": "; ".join(decision.reasons)}})
                        return
                    ledger.append(UsageEvent(request.request_id, "started", actual_cost_usd=0,
                                             cost_accuracy="local_zero", source_event="http_gateway"))
                    upstream_request = urllib.request.Request(
                        gateway.upstream + self.path, data=raw,
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    with gateway.opener(upstream_request, timeout=1200) as response:
                        self.send_response(response.status)
                        self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                        self.send_header("X-MoliCoder-Request-ID", request.request_id)
                        self.end_headers()
                        response_started = True
                        if request.stream:
                            for line in response:
                                self.wfile.write(line)
                                self.wfile.flush()
                            input_tokens = output_tokens = None
                        else:
                            body = response.read()
                            self.wfile.write(body)
                            result = json.loads(body)
                            usage = result.get("usage", {}) if isinstance(result, dict) else {}
                            input_tokens = usage.get("prompt_tokens")
                            output_tokens = usage.get("completion_tokens")
                    ledger.append(UsageEvent(
                        request.request_id, "completed", input_tokens, output_tokens,
                        actual_cost_usd=0, cost_accuracy="local_zero", source_event="http_gateway",
                    ))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    if request_id and ledger:
                        ledger.append(UsageEvent(request_id, "failed", source_event="http_gateway"))
                    if not response_started:
                        self._json(400, {"error": {"message": str(exc)[:200]}})
                    else:
                        self.close_connection = True
                except (OSError, urllib.error.URLError):
                    if request_id and ledger:
                        ledger.append(UsageEvent(request_id, "failed", source_event="http_gateway"))
                    if not response_started:
                        self._json(502, {"error": {"message": "local inference service unavailable"}})
                    else:
                        self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
