"""Capability, override, and enforceable budget boundaries in Build routing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.inference.adapters.ollama import ensure_local_offering, local_offering_id
from local_model_chat.inference.budget import BudgetDenied, BudgetManager
from local_model_chat.inference.catalog import CatalogRecord, import_catalog_record
from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
from local_model_chat.inference.policy import ControlledGateway, InferencePolicy
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.inference.router import RouteRequest, choose_route
from local_model_chat.intent import IntentKind
from local_model_chat.molicoder import runtime
from local_model_chat.molicoder.providers import ProviderResult, ProviderStatus
from local_model_chat.storage import ChatStore
from test_build_plan_approval import approve, fixture, git


def _local_routes(tmp_path):
    store = ChatStore(tmp_path / "routes.sqlite3")
    registry = ProviderRegistry(store)
    for model in ("a-basic", "z-capable"):
        identifier = ensure_local_offering(registry, model, context_window=4096, max_output_tokens=1024)
        registry.set_offering_state(identifier, status="eligible", health_state="healthy",
                                    actor="scenario", reason="fixture model available")
    capable = local_offering_id("z-capable")
    import_catalog_record(store, CatalogRecord(
        capable, "https://example.test/capabilities", datetime.now(timezone.utc).isoformat(), "",
        {"tools": True, "stream": True, "structured_output": True,
         "context_window": 4096, "max_output_tokens": 1024},
        "restricted", True, None, None, {},
    ))
    return registry, local_offering_id("a-basic"), capable


def test_s042_complex_task_excludes_model_without_required_capability(tmp_path):
    registry, basic, capable = _local_routes(tmp_path)
    decision = choose_route(registry, RouteRequest(
        "Implement a structured code change", None,
        required_capabilities=("structured_output",),
        explicit_intent=IntentKind.EXECUTION,
    ))
    assert decision.offering_id == capable
    assert any(basic in item and "missing capability" in item for item in decision.alternatives)


def test_s046_insufficient_hard_budget_prevents_metered_dispatch(tmp_path):
    store = ChatStore(tmp_path / "budget.sqlite3")
    project = store.upsert_project("Fixture", str(tmp_path), "python")
    budget = BudgetManager(store)
    budget.set_project_limit(project.id, 0.0001, actor="user")
    budget.set_scope_limit(project.id, "task", "build-task", 0.00005, actor="user")
    with pytest.raises(BudgetDenied, match="budget"):
        budget.reserve("build-request-1", project.id, 0.001, task_scope="build-task")
    assert budget.active_amount("nonexistent", "build-request-1", project.id) is None
    registry = ProviderRegistry(store)
    registry.add_provider("host", "Host", "https://host.example")
    registry.add_model("coder", "coder", "v1")
    registry.add_offering("paid", "host", "coder", "direct", "metered")
    import_catalog_record(store, CatalogRecord(
        "paid", "https://host.example/prices", datetime.now(timezone.utc).isoformat(),
        "https://host.example/terms", {"tools": True, "context_window": 4096,
        "max_output_tokens": 1024}, "private", True, 1.0, 2.0, {},
    ))
    registry.set_offering_state("paid", status="eligible", health_state="healthy",
                                actor="scenario", reason="fixture route ready")
    store.record_action_authorization(
        project_id=project.id, action="inference_route", scope="paid", risk="financial",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )

    class Adapter:
        calls = 0

        def invoke(self, _request):
            self.calls += 1
            raise AssertionError("an over-budget request reached the metered adapter")

    adapter = Adapter()
    gateway = ControlledGateway(InferencePolicy(registry), adapter)
    request = InferenceRequest(
        "build-request-1", project.id, "paid", "coder", InferencePurpose.EXECUTION,
        ({"role": "user", "content": "Implement build task"},), "private", ("tools",),
        100, 20, 30,
    )
    with pytest.raises(PermissionError, match="budget reservation"):
        gateway.invoke(request)
    assert adapter.calls == 0


def test_s047_planner_model_override_does_not_override_execution(tmp_path, monkeypatch):
    root, store, project, _old_provider, _old_orchestrator = fixture(tmp_path)
    (root / "test_selected.py").write_text(
        "from pathlib import Path\n"
        "def test_selected_output():\n"
        "    assert Path('selected.txt').read_text() == 'selected\\n'\n",
        encoding="utf-8",
    )
    git(root, "add", "test_selected.py")
    git(root, "commit", "-m", "independent output check")
    calls = []

    class Provider:
        name = "codex"

        def __init__(self, model):
            self.model = model

        def probe(self):
            return ProviderStatus("codex", True, True, model=self.model, auth_verified=True)

        def plan(self, _packet):
            from local_model_chat.molicoder.planner.schema import parse_plan
            calls.append(("plan", self.model))
            return parse_plan({"objective": "Write selected model output", "tasks": [{
                "key": "write", "title": "Write", "objective": "Write selected.txt", "risk": "low",
                "depends_on": [], "expected_paths": ["selected.txt"],
                "artifact_checks": [{"kind": "file_equals", "path": "selected.txt", "content": "selected\n"}],
            }]})

        def execute(self, worktree, _prompt, **_kwargs):
            calls.append(("execute", self.model))
            (worktree / "selected.txt").write_text("selected\n", encoding="utf-8")
            return ProviderResult("completed", "done", seconds=0.01, model=self.model)

        def cancel(self):
            pass

    monkeypatch.setattr(runtime, "create_provider", lambda _name, *, model, project_root: Provider(model))
    from local_model_chat.orchestration.catalog import CapabilityCatalogService
    monkeypatch.setattr(CapabilityCatalogService, 'codex_models', staticmethod(lambda: [dict(
        model='economical-executor', description='Affordable for easier tasks', effort_levels=['low','medium'])]))
    store.set_project_preferences(project.id, provider='codex', strategy='FRONTIER_ONLY', trusted_providers=('codex',))

    class Client:
        def tags(self):
            return []

    scope = "scenario-model-override"
    store.record_action_authorization(
        project_id=project.id, action="build_run", scope=scope,
        risk="external", decision="approved", actor="scenario_harness",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    orchestrator = runtime.build_orchestrator(
        store=store, project=project, client=Client(), mode="FRONTIER_ONLY",
        provider_name="codex", model="user-selected-model", authorization_scope=scope,
    )
    orchestrator.worktree_base = tmp_path / "worktrees"
    prepared = orchestrator.run("Write selected model output", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"
    assert store.get_run(prepared.run_id)["model"] == "user-selected-model"
    assert store.get_latest_plan_record(prepared.run_id)["payload"]["task_assignments"]["write"]["model"] == "economical-executor"
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert calls == [("plan", "user-selected-model"), ("execute", "economical-executor")]
    assert not (root / "selected.txt").exists()


def test_s048_incompatible_explicit_model_is_rejected_before_execution(tmp_path):
    registry, basic, _capable = _local_routes(tmp_path)
    decision = choose_route(registry, RouteRequest(
        "Implement a structured code change", None,
        required_capabilities=("structured_output",),
        explicit_intent=IntentKind.EXECUTION,
        candidate_offering_ids=(basic,),
    ))
    assert decision.kind == "clarify" and decision.offering_id is None
    assert any("missing capability" in item for item in decision.alternatives)
