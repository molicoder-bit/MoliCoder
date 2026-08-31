from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class Conversation:
    id: int
    title: str
    model: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class Message:
    id: int | None
    conversation_id: int
    role: str
    content: str
    thinking: str = ""
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass(frozen=True, slots=True)
class Attachment:
    path: Path
    name: str
    size: int
    text: str
    sha256: str


@dataclass(slots=True)
class GenerationMetrics:
    prompt_tokens: int = 0
    output_tokens: int = 0
    prompt_tokens_per_second: float = 0.0
    output_tokens_per_second: float = 0.0
    load_seconds: float = 0.0
    total_seconds: float = 0.0
    done_reason: str = ""


@dataclass(frozen=True, slots=True)
class Project:
    id: int
    name: str
    path: str
    kind: str
    created_at: str
    last_opened_at: str


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: int
    run_id: int
    key: str
    title: str
    objective: str
    status: str
    risk: str
    attempts: int
    worktree: str | None = None
    branch: str | None = None
    base_commit: str | None = None
    changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    run_id: int
    task_id: int | None
    kind: str
    payload_json: str
    created_at: str
