from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from ..storage import ChatStore
from .contracts import InferenceRequest


@dataclass(frozen=True, slots=True)
class UsageEvent:
    request_id: str
    status: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    actual_cost_usd: float | None = None
    cost_accuracy: str = "unknown"
    source_event: str = ""
    provider_request_id: str = ""
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.status not in {"started", "completed", "failed", "denied", "cancelled"}:
            raise ValueError("invalid inference usage status")
        for value in (self.input_tokens, self.output_tokens, self.cached_tokens, self.reasoning_tokens):
            if value is not None and value < 0:
                raise ValueError("token count cannot be negative")
        for value in (self.estimated_cost_usd, self.actual_cost_usd):
            if value is not None and value < 0:
                raise ValueError("cost cannot be negative")
        if self.cost_accuracy not in {"unknown", "estimated", "provider_reported", "local_zero"}:
            raise ValueError("invalid cost accuracy")


class InferenceLedger:
    """Append-only request metadata and usage transitions; never persist message bodies."""

    def __init__(self, store: ChatStore) -> None:
        self.store = store

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def start(self, request: InferenceRequest) -> None:
        with closing(self.store._connect()) as connection:
            connection.execute(
                """INSERT INTO inference_requests(
                    request_id, project_id, run_id, offering_id, model_id, purpose, sensitivity,
                    context_packet_id, reservation_id, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request.request_id, request.project_id, request.run_id, request.offering_id, request.model_id,
                 request.purpose.value, request.sensitivity, request.context_packet_id,
                 request.reservation_id, request.idempotency_key, self._now()),
            )
            connection.commit()

    def append(self, event: UsageEvent) -> None:
        # Source fields are opaque identifiers, not prompt text or provider error messages.
        if len(event.source_event) > 200 or len(event.provider_request_id) > 200:
            raise ValueError("usage source identifier is too long")
        with closing(self.store._connect()) as connection:
            connection.execute(
                """INSERT INTO inference_usage_events(
                    request_id, status, input_tokens, output_tokens, estimated_cost_usd,
                    actual_cost_usd, cost_accuracy, source_event, provider_request_id,
                    cached_tokens, reasoning_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event.request_id, event.status, event.input_tokens, event.output_tokens,
                 event.estimated_cost_usd, event.actual_cost_usd, event.cost_accuracy,
                 event.source_event, event.provider_request_id,
                 event.cached_tokens, event.reasoning_tokens, self._now()),
            )
            connection.commit()

    def events(self, request_id: str) -> list[dict]:
        with closing(self.store._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM inference_usage_events WHERE request_id = ? ORDER BY id",
                (request_id,),
            ).fetchall()
        return [dict(row) for row in rows]
