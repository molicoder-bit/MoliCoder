from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class KandevMode(StrEnum):
    DISABLED = "disabled"
    ATTACHED = "attached"
    MANAGED = "managed"


@dataclass(frozen=True, slots=True)
class KandevConnection:
    mode: KandevMode = KandevMode.DISABLED
    endpoint: str = ""
    version: str = ""
    capabilities: tuple[str, ...] = ()
    auth_reference: str = ""
    owner: str = ""
    health_state: str = "unknown"

    def __post_init__(self) -> None:
        if self.mode == KandevMode.DISABLED and any(
            (self.endpoint, self.version, self.capabilities, self.auth_reference, self.owner)
        ):
            raise ValueError("disabled Kandev connection cannot carry active connection details")
        if self.mode != KandevMode.DISABLED and not self.endpoint:
            raise ValueError("enabled Kandev connection requires an endpoint")


@dataclass(frozen=True, slots=True)
class KandevTaskRef:
    run_id: int
    task_id: int
    external_id: str
    remote_task_id: str = ""


@dataclass(frozen=True, slots=True)
class KandevTaskSnapshot:
    ref: KandevTaskRef
    state: str
    observed_at: str
    session_id: str = ""
    worktree: str = ""
    event_cursor: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class KandevEvent:
    ref: KandevTaskRef
    cursor: str
    kind: str
    observed_at: str
    payload: dict[str, object]


class KandevAdapter(Protocol):
    """Version-gated remote task ownership. Implementations arrive in I12-02/I12-04."""

    def probe(self) -> KandevConnection: ...
    def list_capabilities(self) -> tuple[str, ...]: ...
    def create_task(self, ref: KandevTaskRef, objective: str) -> KandevTaskSnapshot: ...
    def get_task(self, ref: KandevTaskRef) -> KandevTaskSnapshot: ...
    def subscribe_events(self, ref: KandevTaskRef, after_cursor: str = "") -> tuple[KandevEvent, ...]: ...
    def cancel_task(self, ref: KandevTaskRef) -> KandevTaskSnapshot: ...
    def reconcile(self, ref: KandevTaskRef) -> KandevTaskSnapshot: ...
