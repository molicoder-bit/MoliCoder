from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Iterator

from ...domain import GenerationMetrics
from ...ollama import OllamaClient
from ..contracts import InferenceEvent, InferenceEventKind, InferenceRequest
from ..registry import ProviderRegistry


def local_offering_id(model: str) -> str:
    return f"ollama-local:{model}"


def ensure_local_offering(
    registry: ProviderRegistry, model: str, *, context_window: int, max_output_tokens: int,
) -> str:
    offering_id = local_offering_id(model)
    if registry.get_offering(offering_id) is None:
        try:
            registry.add_provider("ollama-local", "Local Ollama", "https://ollama.com")
        except sqlite3.IntegrityError:
            pass
        try:
            registry.add_model(model, model.split(":", 1)[0], model)
        except sqlite3.IntegrityError:
            pass
        registry.add_offering(offering_id, "ollama-local", model, model, "local")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(registry.store._connect()) as connection:
        connection.execute(
            """UPDATE inference_offerings SET capabilities_json = ?, privacy_class = 'restricted',
                   automation_allowed = 1, updated_at = ? WHERE id = ?""",
            (json.dumps({
                "stream": True, "tools": True, "context_window": context_window,
                "max_output_tokens": max_output_tokens,
            }), now, offering_id),
        )
        connection.commit()
    return offering_id


class OllamaStreamAdapter:
    def __init__(self, client: OllamaClient) -> None:
        self.client = client

    def probe_capabilities(self) -> dict[str, bool]:
        return {"stream": True}

    def invoke(self, _request: InferenceRequest):
        raise NotImplementedError("local chat currently uses streaming")

    def stream(self, request: InferenceRequest) -> Iterator[InferenceEvent]:
        now = lambda: datetime.now(timezone.utc).isoformat(timespec="microseconds")
        sequence = 0
        yield InferenceEvent(request.request_id, sequence, InferenceEventKind.STARTED, now())
        options = dict(request.options)
        options["max_output_tokens"] = request.max_output_tokens
        generator = self.client.stream_chat(request.model_id, list(request.messages), **options)
        try:
            while True:
                try:
                    chunk = next(generator)
                except StopIteration as stop:
                    sequence += 1
                    yield InferenceEvent(
                        request.request_id, sequence, InferenceEventKind.COMPLETED, now(),
                        {"metrics": stop.value or GenerationMetrics()},
                    )
                    return
                message = chunk.get("message", {})
                for kind, key in (
                    (InferenceEventKind.CONTENT_DELTA, "content"),
                    (InferenceEventKind.REASONING_DELTA, "thinking"),
                ):
                    value = str(message.get(key, ""))
                    if value:
                        sequence += 1
                        yield InferenceEvent(request.request_id, sequence, kind, now(), {"text": value})
        finally:
            generator.close()

    def cancel(self, _request_id: str) -> None:
        return
