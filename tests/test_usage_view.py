from dataclasses import replace
from datetime import datetime, timezone

from local_model_chat.inference.adapters.ollama import ensure_local_offering
from local_model_chat.inference.budget import BudgetManager
from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
from local_model_chat.inference.ledger import InferenceLedger, UsageEvent
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.inference.usage_view import render_usage_report
from local_model_chat.storage import ChatStore


def test_usage_view_keeps_estimates_unknowns_and_limits_distinct(tmp_path):
    store = ChatStore(tmp_path / "usage.sqlite3")
    root = tmp_path / "project"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    registry = ProviderRegistry(store)
    ensure_local_offering(registry, "test:latest", context_window=100, max_output_tokens=20)
    budgets = BudgetManager(store)
    budgets.set_project_limit(project.id, 5, actor="user", premium_reserve_usd=1)
    budgets.set_scope_limit(project.id, "task", "T1", 0.5, actor="user")
    ledger = InferenceLedger(store)
    request = InferenceRequest(
        request_id="known", project_id=project.id, offering_id="ollama-local:test:latest",
        model_id="test:latest", purpose=InferencePurpose.CHAT,
        messages=({"role": "user", "content": "hello"},), sensitivity="private",
        required_capabilities=(), max_input_tokens=5, max_output_tokens=5, timeout_seconds=60,
    )
    ledger.start(request)
    ledger.append(UsageEvent("known", "completed", 4, 2, actual_cost_usd=0,
                             cost_accuracy="local_zero", source_event="adapter"))
    ledger.start(replace(request, request_id="unknown"))
    ledger.append(UsageEvent("unknown", "completed", 3, 1,
                             cost_accuracy="unknown", source_event="adapter"))
    report = render_usage_report(store, registry, project.id, now=datetime.now(timezone.utc))
    assert "Today: 2 requests" in report
    assert "reported/local cost $0.0000 · estimate $0.0000 · 1 unknown cost" in report
    assert "Project hard API limit: $5.0000; premium reserve $1.0000" in report
    assert "T1 $0.5000" in report
    assert "internal usage and cost unknown" in report
    assert "Accepted-task cost: unknown" in report
    assert "reset unknown" in report
