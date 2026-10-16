from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol

from ..storage import ChatStore
from .contracts import KandevConnection, KandevEvent, KandevMode, KandevTaskRef, KandevTaskSnapshot
from .rest import KandevTransportError


class KandevTaskApi(Protocol):
    """Negotiated Kandev operations; HTTP/MCP transport is supplied by a verified connector."""

    def capabilities(self) -> tuple[str, ...]: ...
    def create(self, external_id: str, objective: str) -> Mapping[str, object]: ...
    def create_and_launch(
        self, external_id: str, objective: str, *,
        blocked_by: tuple[str, ...] = (), agent_profile_id: str = "",
    ) -> Mapping[str, object]: ...
    def read(self, remote_task_id: str) -> Mapping[str, object] | None: ...
    def read_with_session(self, remote_task_id: str) -> Mapping[str, object] | None: ...
    def find_by_external_id(self, external_id: str) -> Mapping[str, object] | None: ...
    def events(self, remote_task_id: str, after_cursor: str) -> tuple[Mapping[str, object], ...]: ...
    def cancel(self, remote_task_id: str) -> Mapping[str, object]: ...


_STATES = {
    "todo": "ready", "created": "ready", "queued": "ready", "scheduling": "ready",
    "in_progress": "running", "running": "running", "review": "verifying",
    "completed": "verifying", "cancelled": "cancelled", "failed": "failed",
    "blocked": "blocked", "waiting_for_input": "review_required",
}


class KandevTaskAdapter:
    """Map Kandev-owned work to local IDs without assuming its internal schema."""

    def __init__(
        self, connection: KandevConnection, api: KandevTaskApi,
        *, store: ChatStore | None = None, on_event: Callable[[KandevEvent], None] | None = None,
    ) -> None:
        self.connection = connection
        self.api = api
        self.store = store
        self.on_event = on_event

    def probe(self) -> KandevConnection:
        health_check = getattr(self.api, "probe_health", None)
        if callable(health_check):
            health_check()
        capabilities = self.api.capabilities()
        required = {"create", "read", "lookup_external_id", "events", "cancel", "launch"}
        health = "ready" if self.connection.mode != KandevMode.DISABLED and required.issubset(capabilities) else "limited"
        return replace(self.connection, capabilities=capabilities, health_state=health)

    def list_capabilities(self) -> tuple[str, ...]:
        return self.api.capabilities()

    @staticmethod
    def _snapshot(ref: KandevTaskRef, raw: Mapping[str, object] | None) -> KandevTaskSnapshot:
        observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not raw:
            return KandevTaskSnapshot(ref, "reconciliation_required", observed, detail="Remote task unavailable")
        remote_id = str(raw.get("id") or "")
        external_id = str(raw.get("external_id") or "")
        if not remote_id or external_id != ref.external_id:
            return KandevTaskSnapshot(ref, "reconciliation_required", observed, detail="Remote identity mismatch")
        resolved = replace(ref, remote_task_id=remote_id)
        if raw.get("creation_complete") is False:
            state = "reconciliation_required"
        else:
            state = _STATES.get(str(raw.get("state") or "").lower(), "reconciliation_required")
        raw_dependencies = raw.get("depends_on")
        dependencies = tuple(
            str(item["id"]) for item in raw_dependencies
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        ) if isinstance(raw_dependencies, list) else ()
        return KandevTaskSnapshot(
            resolved, state, observed, str(raw.get("primary_session_id") or raw.get("session_id") or ""),
            str(raw.get("worktree_path") or ""), str(raw.get("event_cursor") or ""),
            "Unknown or unsettled remote state" if state == "reconciliation_required" else "",
            str(raw.get("worktree_branch") or ""), str(raw.get("base_commit_sha") or ""),
            str(raw.get("repository_id") or ""),
            dependencies,
            str(raw.get("agent_profile_id") or raw.get("primary_agent_profile_id") or ""),
            raw.get("start_when_unblocked") is True,
            str(raw.get("model") or ""),
        )

    def create_task(
        self, ref: KandevTaskRef, objective: str, *,
        blocked_by: tuple[str, ...] = (), agent_profile_id: str = "",
    ) -> KandevTaskSnapshot:
        if self.probe().health_state != "ready":
            raise RuntimeError("Kandev task capabilities are incomplete")
        try:
            raw = (
                self.api.create_and_launch(
                    ref.external_id, objective,
                    blocked_by=blocked_by, agent_profile_id=agent_profile_id,
                ) if blocked_by or agent_profile_id else
                self.api.create_and_launch(ref.external_id, objective)
            )
        except KandevTransportError:
            # A transport failure may happen after Kandev accepted the create.
            # The stable external ID is the only safe recovery key; never retry
            # launch or silently hand the task to the internal Build engine.
            raw = self.api.find_by_external_id(ref.external_id)
            if raw is None:
                raise
        return self._snapshot(ref, raw)

    def get_task(self, ref: KandevTaskRef) -> KandevTaskSnapshot:
        if ref.remote_task_id:
            raw = self.api.read_with_session(ref.remote_task_id)
        else:
            raw = self.api.find_by_external_id(ref.external_id)
            if raw and isinstance(raw.get("id"), str):
                raw = self.api.read_with_session(raw["id"])
        return self._snapshot(ref, raw)

    def subscribe_events(self, ref: KandevTaskRef, after_cursor: str = "") -> tuple[KandevEvent, ...]:
        if not ref.remote_task_id:
            return ()
        if not after_cursor and self.store is not None:
            for recorded in reversed(self.store.list_events(ref.run_id)):
                if recorded.task_id != ref.task_id or recorded.kind != "kandev_message":
                    continue
                try:
                    payload = json.loads(recorded.payload_json)
                except json.JSONDecodeError:
                    continue
                if payload.get("remote_task_id") == ref.remote_task_id and isinstance(payload.get("cursor"), str):
                    after_cursor = payload["cursor"]
                    break
        events = []
        seen_cursors = {after_cursor}
        if self.store is not None:
            for recorded in self.store.list_events(ref.run_id):
                if recorded.task_id != ref.task_id or not recorded.kind.startswith("kandev_"):
                    continue
                try:
                    payload = json.loads(recorded.payload_json)
                except json.JSONDecodeError:
                    continue
                if payload.get("remote_task_id") == ref.remote_task_id and isinstance(payload.get("cursor"), str):
                    seen_cursors.add(payload["cursor"])
        for raw in self.api.events(ref.remote_task_id, after_cursor):
            cursor = str(raw.get("cursor") or "")
            if not cursor or cursor in seen_cursors:
                continue
            seen_cursors.add(cursor)
            payload = raw.get("payload")
            event = KandevEvent(
                ref, cursor, str(raw.get("kind") or "unknown"),
                str(raw.get("observed_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
                dict(payload) if isinstance(payload, Mapping) else {},
            )
            events.append(event)
            if self.store is not None:
                self.store.add_event(ref.run_id, "kandev_" + event.kind, {
                    "external_id": ref.external_id, "remote_task_id": ref.remote_task_id,
                    "cursor": event.cursor, "observed_at": event.observed_at,
                    "payload": event.payload,
                }, ref.task_id)
            if self.on_event:
                self.on_event(event)
        return tuple(events)

    def cancel_task(self, ref: KandevTaskRef) -> KandevTaskSnapshot:
        if not ref.remote_task_id:
            return self.get_task(ref)
        self.api.cancel(ref.remote_task_id)
        # A successful cancellation request does not prove the remote task stopped.
        return self.get_task(ref)

    def reconcile(self, ref: KandevTaskRef) -> KandevTaskSnapshot:
        return self.get_task(ref)
