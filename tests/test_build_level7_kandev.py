"""Kandev ownership preflight against the pinned adapter contract."""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from local_model_chat.kandev.adapter import KandevTaskAdapter
from local_model_chat.kandev.contracts import KandevConnection, KandevMode
from local_model_chat.kandev.rest import KandevRestConfig, KandevRestTaskApi
from local_model_chat.kandev.run import KandevRunOrchestrator
from local_model_chat.environment.maintenance import active_version
from local_model_chat.molicoder.executor import ExecutionResult
from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderUnavailable
from local_model_chat.molicoder.repair import RepairOutcome
from local_model_chat.molicoder.runtime import build_orchestrator
from test_build_plan_approval import approve, fixture, git


class LocalClient:
    def tags(self):
        return [{"name": "molicoder:latest"}]


class ProbeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        assert timeout == 5
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode("utf-8"))


def _connection():
    config = KandevRestConfig(
        "http://127.0.0.1:38429/mcp", "0.97.0", "workspace-1", "workflow-1",
        "step-1", "repo-1", "profile-1",
    )
    connection = KandevConnection(KandevMode.ATTACHED, config.endpoint, config.version)
    return config, connection


def test_s071_disabled_kandev_executes_internal_build_without_adapter_call(tmp_path):
    root, store, project, _provider, _old_orchestrator = fixture(tmp_path)
    (root / "greeting.txt").write_text("old\n", encoding="utf-8")
    git(root, "add", "greeting.txt")
    git(root, "commit", "-m", "existing file")

    class NeverContactKandev:
        def probe(self):
            raise AssertionError("disabled Kandev must not be contacted")

    orchestrator = build_orchestrator(
        store=store, project=project, client=LocalClient(), mode="LOCAL_OFFLINE",
        kandev_connection=KandevConnection(), kandev_adapter=NeverContactKandev(),
    )
    assert isinstance(orchestrator, SequentialRunOrchestrator)

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Edit greeting", "tasks": [{
                "key": "edit", "title": "Edit greeting", "objective": "Edit greeting.txt",
                "risk": "low", "depends_on": [], "expected_paths": ["greeting.txt"],
            }]})

    calls = []

    def local_executor(worktree, _prompt):
        calls.append(1)
        (Path(worktree) / "greeting.txt").write_text("new\n", encoding="utf-8")
        execution = ExecutionResult("completed", "done", 1, 1)
        return RepairOutcome("completed", 1, execution, (), "", (execution,))

    orchestrator.planner = Planner()
    orchestrator.local_executor = local_executor
    orchestrator.validate = lambda path: (
        "validation passed" if (Path(path) / "greeting.txt").read_text(encoding="utf-8") == "new\n"
        else "validation failed"
    )
    orchestrator.validate_task = lambda path, _task: orchestrator.validate(path)
    prepared = orchestrator.run("Edit greeting.txt", mode="LOCAL_OFFLINE")
    assert prepared.status == "awaiting_approval" and calls == []
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert calls == [1] and (root / "greeting.txt").read_text(encoding="utf-8") == "old\n"


def test_s072_enabled_without_adapter_gives_setup_guidance_and_no_run(tmp_path):
    _root, store, project, _provider, _orchestrator = fixture(tmp_path)
    assert active_version(tmp_path / "empty-managed-kandev", "kandev") is None
    config, _ = _connection()
    connection = KandevConnection(KandevMode.MANAGED, config.endpoint, config.version)
    with pytest.raises(ProviderUnavailable, match="configure the local Kandev connection"):
        build_orchestrator(
            store=store, project=project, client=LocalClient(), mode="LOCAL_OFFLINE",
            kandev_connection=connection,
        )
    assert store.list_project_runs(project.id) == []


@pytest.mark.parametrize("failure,expected,requests", [
    ("disconnected", "Kandev endpoint is unavailable", 1),
    ("unauthorized", "Kandev returned HTTP 401", 2),
])
def test_s073_disconnected_or_unauthorized_kandev_is_not_ready(tmp_path, failure, expected, requests):
    _root, store, project, _provider, _orchestrator = fixture(tmp_path)
    config, connection = _connection()
    api = KandevRestTaskApi(config)
    opener = ProbeOpener(
        [URLError("connection refused")] if failure == "disconnected" else [
            {"status": "ok", "version": "v0.97.0"},
            HTTPError(config.endpoint, 401, "unauthorized", {}, None),
        ]
    )
    api._opener = opener
    adapter = KandevTaskAdapter(connection, api, store=store)
    with pytest.raises(ProviderUnavailable, match=expected):
        build_orchestrator(
            store=store, project=project, client=LocalClient(), mode="LOCAL_OFFLINE",
            kandev_connection=connection, kandev_adapter=adapter,
        )
    assert len(opener.requests) == requests and all(request.get_method() == "GET" for request in opener.requests)
    assert store.list_project_runs(project.id) == []


def test_s074_successful_read_only_probe_discovers_capabilities(tmp_path):
    _root, store, project, _provider, _orchestrator = fixture(tmp_path)
    config, connection = _connection()
    api = KandevRestTaskApi(config)
    opener = ProbeOpener([
        {"status": "ok", "version": "v0.97.0"},
        HTTPError(config.endpoint, 404, "missing", {}, None),
    ] * 2)
    api._opener = opener
    adapter = KandevTaskAdapter(connection, api, store=store)
    discovered = adapter.probe()
    assert discovered.health_state == "ready"
    assert {"create", "read", "lookup_external_id", "events", "cancel", "launch"}.issubset(
        discovered.capabilities
    )
    assert len(opener.requests) == 2
    assert opener.requests[0].full_url == "http://127.0.0.1:38429/health"
    assert opener.requests[1].full_url.endswith(
        "/workspaces/workspace-1/tasks/by-external-id?external_id=molicoder%3Aconnection-probe%3Aabsent"
    )
    orchestrator = build_orchestrator(
        store=store, project=project, client=LocalClient(), mode="LOCAL_OFFLINE",
        kandev_connection=connection, kandev_adapter=adapter,
    )
    assert isinstance(orchestrator, KandevRunOrchestrator)
    assert len(opener.requests) == 4 and store.list_project_runs(project.id) == []
