from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.inference.adapters.cli import BoundedOpaqueProvider
from local_model_chat.molicoder.providers import ProviderResult, ProviderStatus, ProviderUnavailable
from local_model_chat.storage import ChatStore


class FakeProvider:
    name = "codex"
    model = "CLI default"

    def __init__(self) -> None:
        self.on_event = None
        self.plans = 0
        self.executions = 0
        self.cancelled = 0
        self.timeouts: list[float] = []

    def probe(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, True)

    def plan(self, _packet):
        self.plans += 1
        return "plan"

    def execute(self, _worktree, _prompt, *, timeout_seconds: float = 1200):
        self.executions += 1
        self.timeouts.append(timeout_seconds)
        return ProviderResult("completed", "done")

    def cancel(self) -> None:
        self.cancelled += 1


def _approve(store: ChatStore, project_id: int, scope: str) -> None:
    store.record_action_authorization(
        project_id=project_id, action="build_run", scope=scope, risk="external",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )


def test_opaque_cli_requires_exact_approval_and_stops_at_call_ceiling(tmp_path) -> None:
    store = ChatStore(tmp_path / "cli.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    provider = FakeProvider()
    adapter = BoundedOpaqueProvider(provider, store, project.id, "exact", max_calls=2, max_seconds=1200)

    with pytest.raises(PermissionError, match="approval"):
        adapter.plan(None)
    assert provider.plans == 0
    _approve(store, project.id, "different")
    with pytest.raises(PermissionError, match="approval"):
        adapter.plan(None)
    _approve(store, project.id, "exact")

    assert adapter.plan(None) == "plan"
    assert adapter.execute(tmp_path, "edit", timeout_seconds=2000).status == "completed"
    assert provider.timeouts[0] <= 1200
    with pytest.raises(ProviderUnavailable, match="ceiling"):
        adapter.plan(None)
    assert provider.plans == 1
    assert adapter.calls == 2
    assert adapter.usage_accuracy == "unknown"
    handler = lambda *_args: None
    adapter.on_event = handler
    assert provider.on_event is handler
    adapter.cancel()
    assert provider.cancelled == 1


def test_opaque_cli_stops_when_duration_ceiling_expires(tmp_path) -> None:
    store = ChatStore(tmp_path / "cli.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    _approve(store, project.id, "exact")
    provider = FakeProvider()
    adapter = BoundedOpaqueProvider(provider, store, project.id, "exact", max_seconds=0.001)
    adapter._started_at -= 1

    with pytest.raises(ProviderUnavailable, match="ceiling"):
        adapter.execute(tmp_path, "edit")
    assert provider.executions == 0
