from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.environment.discovery import EnvironmentReport
from local_model_chat.environment.models import plan_local_model, pull_approved_local_model
from local_model_chat.storage import ChatStore


def _environment(memory: int, disk: int) -> EnvironmentReport:
    return EnvironmentReport(1, "Windows", "x86_64", 8, memory, disk,
                             "unknown", "fixture", (), ())


class FakeClient:
    def __init__(self):
        self.pulls = []

    def pull_model(self, model):
        self.pulls.append(model)


def test_model_plan_shows_ram_vram_disk_and_requires_exact_approval(tmp_path) -> None:
    store = ChatStore(tmp_path / "models.sqlite3")
    client = FakeClient()
    plan = plan_local_model(
        _environment(16 * 1024**3, 40 * 1024**3), "small-model:latest",
        4 * 1024**3, source="https://ollama.com/library/small-model",
    )
    assert plan.suitable
    assert plan.ram_needed_bytes == 8 * 1024**3
    assert plan.vram_preferred_bytes == 4 * 1024**3
    assert plan.disk_needed_bytes == 8 * 1024**3
    with pytest.raises(PermissionError, match="approval"):
        pull_approved_local_model(store, client, plan)
    assert client.pulls == []

    store.record_action_authorization(
        project_id=None, action="pull_local_model", scope=plan.scope,
        risk="mutation", decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    pull_approved_local_model(store, client, plan)
    assert client.pulls == ["small-model:latest"]


def test_unsuitable_model_cannot_download_even_with_approval(tmp_path) -> None:
    store = ChatStore(tmp_path / "models.sqlite3")
    client = FakeClient()
    plan = plan_local_model(
        _environment(2 * 1024**3, 2 * 1024**3), "large-model:latest",
        4 * 1024**3, source="https://ollama.com/library/large-model",
    )
    assert not plan.suitable
    with pytest.raises(PermissionError, match="RAM"):
        pull_approved_local_model(store, client, plan)
    assert client.pulls == []
