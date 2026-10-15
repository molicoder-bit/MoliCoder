from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.environment.discovery import EnvironmentReport
from local_model_chat.environment.models import plan_local_model, pull_approved_local_model
from local_model_chat.environment.models import official_model_size
import io
import json
from local_model_chat.storage import ChatStore


def _environment(memory: int, disk: int) -> EnvironmentReport:
    return EnvironmentReport(1, "Windows", "x86_64", 8, memory, disk,
                             "unknown", "fixture", (), ())


class FakeClient:
    def __init__(self):
        self.pulls = []

    def pull_model(self, model):
        self.pulls.append(model)


def test_official_model_manifest_size_is_bounded_and_validated() -> None:
    payload = json.dumps({"layers": [
        {"size": 400, "digest": "sha256:" + "a" * 64},
        {"size": 20, "digest": "sha256:" + "b" * 64},
    ]}).encode()
    seen = []

    def open_manifest(request, *, timeout):
        seen.append((request.full_url, timeout))
        return io.BytesIO(payload)

    assert official_model_size("qwen2.5-coder:7b", opener=open_manifest) == (
        420, "https://registry.ollama.ai/v2/library/qwen2.5-coder/manifests/7b",
    )
    assert seen == [("https://registry.ollama.ai/v2/library/qwen2.5-coder/manifests/7b", 10)]
    with pytest.raises(ValueError, match="public Ollama library"):
        official_model_size("example.com/private/model", opener=open_manifest)
    with pytest.raises(ValueError, match="size limit"):
        official_model_size("qwen2.5-coder:7b", opener=lambda *_args, **_kwargs: io.BytesIO(b"x" * (256 * 1024 + 1)))
    with pytest.raises(ValueError, match="invalid layer"):
        official_model_size("qwen2.5-coder:7b", opener=lambda *_args, **_kwargs: io.BytesIO(b'{"layers":[{"size":-1,"digest":"bad"}]}'))


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
