from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterator, Protocol


class InferencePurpose(StrEnum):
    CHAT = "chat"
    ANALYSIS = "analysis"
    PLANNING = "planning"
    EXECUTION = "execution"
    REVIEW = "review"


class InferenceError(StrEnum):
    AUTH = "auth"
    PERMISSION = "permission"
    QUOTA = "quota"
    BUDGET = "budget"
    CAPABILITY = "capability"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class InferenceEventKind(StrEnum):
    STARTED = "started"
    CONTENT_DELTA = "content_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL_PROPOSED = "tool_call_proposed"
    USAGE_DELTA = "usage_delta"
    HEARTBEAT = "heartbeat"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    request_id: str
    project_id: int | None
    offering_id: str
    model_id: str
    purpose: InferencePurpose
    messages: tuple[dict[str, Any], ...]
    sensitivity: str
    required_capabilities: tuple[str, ...]
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: float
    context_packet_id: str | None = None
    stream: bool = False
    tool_scopes: tuple[str, ...] = ()
    policy_revision: str = "default"
    reservation_id: str | None = None
    idempotency_key: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    run_id: int | None = None
    task_id: int | None = None

    def __post_init__(self) -> None:
        if not self.request_id or not self.offering_id or not self.model_id:
            raise ValueError("request, offering, and model IDs are required")
        if not self.messages or self.max_input_tokens <= 0 or self.max_output_tokens <= 0:
            raise ValueError("messages and positive input/output limits are required")
        if self.timeout_seconds <= 0 or self.sensitivity not in {"public", "private", "restricted"}:
            raise ValueError("timeout and sensitivity must be valid")
        if self.task_id is not None and (self.run_id is None or self.task_id <= 0):
            raise ValueError("task attribution requires a run and positive task ID")


@dataclass(frozen=True, slots=True)
class InferenceEvent:
    request_id: str
    sequence: int
    kind: InferenceEventKind
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id or self.sequence < 0 or not self.timestamp:
            raise ValueError("inference event needs request ID, sequence, and timestamp")


@dataclass(frozen=True, slots=True)
class InferenceResult:
    request_id: str
    status: str
    provider_id: str
    model_id: str
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    provider_request_id: str | None = None
    first_token_seconds: float | None = None
    total_seconds: float | None = None
    error: InferenceError | None = None


class InferenceAdapter(Protocol):
    def probe_capabilities(self) -> dict[str, bool | int | str]: ...
    def invoke(self, request: InferenceRequest) -> InferenceResult: ...
    def stream(self, request: InferenceRequest) -> Iterator[InferenceEvent]: ...
    def cancel(self, request_id: str) -> None: ...
