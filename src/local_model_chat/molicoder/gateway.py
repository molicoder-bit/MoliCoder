from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from ..domain import GenerationMetrics
from ..ollama import OllamaClient


@dataclass(frozen=True, slots=True)
class InferenceResponse:
    message: dict[str, Any]
    metrics: GenerationMetrics


class InferenceGateway:
    """The single serialized lane for local inference."""

    def __init__(self, client: OllamaClient | None = None) -> None:
        self.client = client or OllamaClient()
        self._lock = threading.Lock()
        self.resident_model: str | None = None

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        context_window: int,
        max_output_tokens: int,
        temperature: float,
        top_p: float,
    ) -> InferenceResponse:
        with self._lock:
            response = self.client.chat(
                model,
                messages,
                tools=tools,
                context_window=context_window,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                top_p=top_p,
            )
            self.resident_model = model
            return InferenceResponse(response.message, response.metrics)

