from datetime import datetime, timezone

from local_model_chat.inference.budget import BudgetManager
from local_model_chat.inference.adapters.ollama import ensure_local_offering
from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
from local_model_chat.inference.ledger import InferenceLedger, UsageEvent
from local_model_chat.inference.pacing import project_pacing
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


def test_pacing_explains_unconfigured_and_fast_burn_without_false_balance(tmp_path) -> None:
    store = ChatStore(tmp_path / "pacing.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    now = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    budget = BudgetManager(store)

    unconfigured = project_pacing(store, project.id, now=now)
    assert unconfigured.state == "unconfigured"
    assert unconfigured.monthly_limit_usd is None

    budget.set_project_limit(project.id, 10, actor="user")
    budget.set_scope_limit(project.id, "month", now.strftime("%Y-%m"), 2, actor="user")
    budget.reserve("request-1", project.id, 1.5)
    snapshot = project_pacing(store, project.id, now=now)

    assert snapshot.committed_usd == 1.5
    assert snapshot.state == "fast_burn"
    assert "not a provider-confirmed balance" in snapshot.explanation


def test_pacing_shows_latest_policy_reason_and_cost_accuracy(tmp_path) -> None:
    store = ChatStore(tmp_path / "pacing.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    ensure_local_offering(ProviderRegistry(store), "test:latest", context_window=100, max_output_tokens=20)
    ledger = InferenceLedger(store)

    def request(request_id: str) -> InferenceRequest:
        return InferenceRequest(
            request_id, project.id, "ollama-local:test:latest", "test:latest",
            InferencePurpose.CHAT, ({"role": "user", "content": "private"},),
            "private", (), 5, 5, 30,
        )

    ledger.start(request("first"))
    ledger.append(UsageEvent("first", "denied", source_event="policy:offering status is unavailable"))
    assert "deferred before dispatch: offering status is unavailable" in project_pacing(store, project.id).explanation

    ledger.start(request("second"))
    ledger.append(UsageEvent("second", "completed", 5, 2, actual_cost_usd=0,
                             cost_accuracy="local_zero", source_event="adapter"))
    assert "local zero provider cost" in project_pacing(store, project.id).explanation
