from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import OLLAMA_BASE_URL
from .domain import GenerationMetrics


class OllamaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChatResponse:
    message: dict[str, Any]
    metrics: GenerationMetrics


class OllamaClient:
    def __init__(self, base_url: str = OLLAMA_BASE_URL, timeout: int = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._server_process: subprocess.Popen[bytes] | None = None

    def _request(self, path: str, payload: dict[str, Any] | None = None, timeout: int | None = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise OllamaError(str(exc)) from exc

    def is_available(self) -> bool:
        try:
            self._request("/api/version", timeout=2)
            return True
        except OllamaError:
            return False

    def ensure_running(self, wait_seconds: float = 15.0) -> bool:
        if self.is_available():
            return True
        executable = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if not executable.exists():
            return False
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._server_process = subprocess.Popen(
                [str(executable), "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError:
            return False
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.is_available():
                return True
            time.sleep(0.5)
        return False

    def tags(self) -> list[dict[str, Any]]:
        return list(self._request("/api/tags").get("models", []))

    def show(self, model: str) -> dict[str, Any]:
        return dict(self._request("/api/show", {"model": model}))

    def pull_model(self, model: str) -> None:
        result = self._request("/api/pull", {"model": model, "stream": False}, timeout=3600)
        if not isinstance(result, dict) or result.get("error") or result.get("status") != "success":
            raise OllamaError("local model download did not complete successfully")

    def running_models(self) -> list[dict[str, Any]]:
        return list(self._request("/api/ps").get("models", []))

    def unload(self, model: str) -> None:
        self._request("/api/generate", {"model": model, "prompt": "", "keep_alive": 0, "stream": False})

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        context_window: int,
        max_output_tokens: int,
        temperature: float,
        top_p: float,
        keep_alive: str = "5m",
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": keep_alive,
            "options": {
                "num_ctx": context_window,
                "num_predict": max_output_tokens,
                "temperature": temperature,
                "top_p": top_p,
            },
        }
        if tools:
            payload["tools"] = tools
        started = time.monotonic()
        response = dict(self._request("/api/chat", payload, timeout=3600))
        if response.get("error"):
            raise OllamaError(str(response["error"]))
        message = response.get("message", {})
        if not isinstance(message, dict):
            raise OllamaError("Ollama returned an invalid chat message")
        return ChatResponse(dict(message), _metrics_from_response(response, time.monotonic() - started))

    def stream_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        context_window: int,
        max_output_tokens: int,
        temperature: float,
        top_p: float,
        show_thinking: bool,
        keep_alive: str = "5m",
    ) -> Generator[dict[str, Any], None, GenerationMetrics]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": show_thinking,
            "keep_alive": keep_alive,
            "options": {
                "num_ctx": context_window,
                "num_predict": max_output_tokens,
                "temperature": temperature,
                "top_p": top_p,
            },
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=3600) as response:
                final: dict[str, Any] = {}
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    chunk = json.loads(raw_line.decode("utf-8"))
                    if chunk.get("error"):
                        raise OllamaError(str(chunk["error"]))
                    final = chunk
                    yield chunk
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise OllamaError(str(exc)) from exc
        return _metrics_from_response(final, time.monotonic() - started)


def _rate(count: int, duration_ns: int) -> float:
    return round(count / (duration_ns / 1_000_000_000), 1) if count and duration_ns else 0.0


def _metrics_from_response(response: dict[str, Any], elapsed: float) -> GenerationMetrics:
    prompt_count = int(response.get("prompt_eval_count", 0))
    output_count = int(response.get("eval_count", 0))
    return GenerationMetrics(
        prompt_tokens=prompt_count,
        output_tokens=output_count,
        prompt_tokens_per_second=_rate(prompt_count, int(response.get("prompt_eval_duration", 0))),
        output_tokens_per_second=_rate(output_count, int(response.get("eval_duration", 0))),
        load_seconds=round(int(response.get("load_duration", 0)) / 1_000_000_000, 2),
        total_seconds=round(elapsed, 2),
        done_reason=str(response.get("done_reason", "")),
    )
