from __future__ import annotations

import threading
import time
from pathlib import Path

from ...authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ...molicoder.planner.schema import Plan
from ...molicoder.providers import FrontierProvider, ProviderResult, ProviderStatus, ProviderUnavailable
from ...molicoder.recon import ReconPacket
from ...storage import ChatStore


class BoundedOpaqueProvider:
    """Limit CLI launches; internal tokens, routes and subscription spend remain opaque."""

    enforcement_class = "opaque_agent"
    usage_accuracy = "unknown"

    def __init__(
        self, provider: FrontierProvider, store: ChatStore, project_id: int,
        authorization_scope: str, *, max_calls: int = 8, max_seconds: float = 3600,
    ) -> None:
        if not authorization_scope or max_calls <= 0 or max_seconds <= 0:
            raise ValueError("opaque provider needs authorization scope and positive ceilings")
        self.provider = provider
        self.enforcement_class = getattr(provider, 'enforcement_class', 'opaque_agent')
        self.store = store
        self.project_id = project_id
        self.authorization_scope = authorization_scope
        self.max_calls = max_calls
        self.max_seconds = max_seconds
        self.name = provider.name
        self.model = provider.model
        self.effort = getattr(provider, 'effort', None)
        self._started_at = time.monotonic()
        self._calls = 0
        self._lock = threading.Lock()

    @property
    def on_event(self):
        return getattr(self.provider, "on_event", None)

    @on_event.setter
    def on_event(self, handler) -> None:
        if hasattr(self.provider, "on_event"):
            self.provider.on_event = handler

    @property
    def calls(self) -> int:
        return self._calls

    def probe(self) -> ProviderStatus:
        return self.provider.probe()

    def _begin_call(self) -> float:
        AuthorizationPolicy(self.store).require(ActionRequest(
            self.project_id, "build_run", self.authorization_scope, ActionRisk.EXTERNAL,
        ))
        with self._lock:
            remaining = self.max_seconds - (time.monotonic() - self._started_at)
            if self._calls >= self.max_calls or remaining <= 0:
                raise ProviderUnavailable("opaque agent call/time ceiling reached")
            self._calls += 1
            return remaining

    def _guard(self, operation):
        remaining = self._begin_call()
        timer = threading.Timer(remaining, self.provider.cancel)
        timer.daemon = True
        timer.start()
        try:
            result = operation(remaining)
            if time.monotonic() - self._started_at > self.max_seconds:
                raise ProviderUnavailable("opaque agent duration ceiling reached")
            return result
        finally:
            timer.cancel()

    def plan(self, packet: ReconPacket) -> Plan:
        return self._guard(lambda _remaining: self.provider.plan(packet))

    def execute(
        self, worktree: str | Path, prompt: str, *, timeout_seconds: float = 1200.0,
    ) -> ProviderResult:
        return self._guard(lambda remaining: self.provider.execute(
            worktree, prompt, timeout_seconds=min(timeout_seconds, remaining),
        ))

    def cancel(self) -> None:
        self.provider.cancel()

    def set_task_scope(self, paths):
        setter = getattr(self.provider, 'set_task_scope', None)
        if setter:
            setter(paths)
