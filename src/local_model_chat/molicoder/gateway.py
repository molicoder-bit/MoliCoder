from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..context import estimate_messages
from ..domain import GenerationMetrics
from ..inference.adapters.ollama import local_offering_id
from ..inference.contracts import InferencePurpose, InferenceRequest
from ..inference.ledger import InferenceLedger, UsageEvent
from ..inference.policy import InferencePolicy
from ..ollama import OllamaClient


@dataclass(frozen=True, slots=True)
class InferenceResponse:
    message: dict[str, Any]
    metrics: GenerationMetrics


class InferenceGateway:
    """The single serialized lane for local inference."""

    def __init__(
        self, client: OllamaClient | None = None, *,
        policy: InferencePolicy | None = None, project_id: int | None = None,
    ) -> None:
        self.client = client or OllamaClient()
        self.policy = policy
        self.project_id = project_id
        self._lock = threading.Lock()
        self.resident_model: str | None = None
        self.last_request_id: str | None = None
        self.run_id: int | None = None
        self.task_id: int | None = None

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
        purpose: InferencePurpose = InferencePurpose.EXECUTION,
    ) -> InferenceResponse:
        request = InferenceRequest(
            request_id=str(uuid4()), project_id=self.project_id,
            offering_id=local_offering_id(model), model_id=model,
            purpose=purpose, messages=tuple(messages),
            sensitivity="private", required_capabilities=(),
            max_input_tokens=max(1, estimate_messages(messages)),
            max_output_tokens=max_output_tokens, timeout_seconds=1200,
            tool_scopes=("local-worktree-tools",) if tools else (),
            options={"context_window": context_window, "temperature": temperature, "top_p": top_p},
            run_id=self.run_id, task_id=self.task_id,
        )
        self.last_request_id = request.request_id
        ledger = InferenceLedger(self.policy.registry.store) if self.policy is not None else None
        if ledger is not None:
            ledger.start(request)
        if self.policy is not None:
            decision = self.policy.evaluate(request)
            if not decision.allowed:
                if ledger is not None:
                    ledger.append(UsageEvent(
                        request.request_id, "denied",
                        source_event=("policy:" + "; ".join(decision.reasons))[:200],
                    ))
                raise PermissionError("; ".join(decision.reasons))
        if ledger is not None:
            ledger.append(UsageEvent(request.request_id, "started", source_event="gateway"))
        with self._lock:
            try:
                response = self.client.chat(
                    model,
                    messages,
                    tools=tools,
                    context_window=context_window,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                    top_p=top_p,
                )
            except Exception:
                if ledger is not None:
                    ledger.append(UsageEvent(request.request_id, "failed", source_event="adapter"))
                raise
            if ledger is not None:
                ledger.append(UsageEvent(
                    request.request_id, "completed", response.metrics.prompt_tokens,
                    response.metrics.output_tokens, actual_cost_usd=0.0,
                    cost_accuracy="local_zero", source_event="adapter",
                ))
            self.resident_model = model
            return InferenceResponse(response.message, response.metrics)

