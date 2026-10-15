import pytest
from datetime import datetime, timedelta, timezone

from local_model_chat.config import MODEL_PRESETS
from local_model_chat.domain import GenerationMetrics
from local_model_chat.inference.adapters.cli import BoundedOpaqueProvider
from local_model_chat.inference.ledger import InferenceLedger
from local_model_chat.molicoder.providers import ProviderStatus, ProviderUnavailable
from local_model_chat.molicoder.runtime import build_orchestrator
from local_model_chat.ollama import ChatResponse
from local_model_chat.storage import ChatStore


class FakeLocalClient:
    def __init__(self, available: bool):
        self.available = available
        self.calls = 0

    def tags(self):
        return [{"name": "molicoder:latest"}] if self.available else []

    def chat(self, *_args, **_kwargs):
        self.calls += 1
        return ChatResponse({"content": "ok"}, GenerationMetrics(prompt_tokens=3, output_tokens=1))


def test_build_local_gateway_authorizes_before_model_call(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = ChatStore(tmp_path / "build-policy.sqlite3")
    project = store.upsert_project("Project", str(root), "python")
    preset = MODEL_PRESETS["molicoder:latest"]
    client = FakeLocalClient(True)
    orchestrator = build_orchestrator(store=store, project=project, client=client, mode="LOCAL_OFFLINE")
    gateway = orchestrator.planner.gateway

    response = gateway.chat(
        model=preset.name, messages=[{"role": "user", "content": "Plan"}], tools=None,
        context_window=preset.context_window, max_output_tokens=20,
        temperature=0.1, top_p=0.9,
    )

    assert response.message["content"] == "ok"
    assert gateway.last_request_id
    assert client.calls == 1
    assert [event["status"] for event in InferenceLedger(store).events(gateway.last_request_id)] == ["started", "completed"]

    missing = FakeLocalClient(False)
    with pytest.raises(ProviderUnavailable, match="no verified eligible route"):
        build_orchestrator(
            store=store, project=project, client=missing, mode="LOCAL_OFFLINE",
        )
    assert missing.calls == 0


def test_build_cli_mode_requires_scope_and_wraps_provider_before_calls(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = ChatStore(tmp_path / "build-policy.sqlite3")
    project = store.upsert_project("Project", str(root), "python")

    class FakeProvider:
        name = "codex"
        model = "CLI default"

        def probe(self):
            return ProviderStatus("codex", True, True)

        def plan(self, _packet):
            raise AssertionError("must authorize first")

        def execute(self, _worktree, _prompt, *, timeout_seconds=1200):
            raise AssertionError("must authorize first")

        def cancel(self):
            pass

    monkeypatch.setattr("local_model_chat.molicoder.runtime.create_provider", lambda *_args, **_kwargs: FakeProvider())
    with pytest.raises(ProviderUnavailable, match="authorization scope"):
        build_orchestrator(store=store, project=project, client=FakeLocalClient(False), mode="HYBRID")

    orchestrator = build_orchestrator(
        store=store, project=project, client=FakeLocalClient(False), mode="HYBRID",
        authorization_scope="exact-build",
    )
    assert isinstance(orchestrator.provider, BoundedOpaqueProvider)
    with pytest.raises(PermissionError, match="approval"):
        orchestrator.provider.plan(None)


def test_fallback_provider_requires_separate_exact_authorization(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = ChatStore(tmp_path / "fallback-policy.sqlite3")
    project = store.upsert_project("Project", str(root), "python")
    store.set_project_preferences(project.id, provider="codex", strategy="HYBRID",
                                  trusted_providers=("codex", "claude"), model_overrides={})

    class FakeProvider:
        model = "fixture"

        def __init__(self, name):
            self.name = name

        def probe(self):
            return ProviderStatus(self.name, True, True, auth_verified=True)

        def cancel(self):
            pass

    monkeypatch.setattr(
        "local_model_chat.molicoder.runtime.create_provider",
        lambda name, **_kwargs: FakeProvider(name),
    )
    with pytest.raises(PermissionError, match="approval"):
        build_orchestrator(
            store=store, project=project, client=FakeLocalClient(False), mode="HYBRID",
            provider_name="codex", authorization_scope="primary-exact",
            fallback_provider_name="claude", fallback_authorization_scope="fallback-exact",
        )
    store.record_action_authorization(
        project_id=project.id, action="build_run", scope="fallback-exact", risk="external",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    orchestrator = build_orchestrator(
        store=store, project=project, client=FakeLocalClient(False), mode="HYBRID",
        provider_name="codex", authorization_scope="primary-exact",
        fallback_provider_name="claude", fallback_authorization_scope="fallback-exact",
    )
    assert isinstance(orchestrator.alternate_provider, BoundedOpaqueProvider)
    assert orchestrator.alternate_provider.name == "claude"
