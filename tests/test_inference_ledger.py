import sqlite3
from dataclasses import replace
import pytest

from local_model_chat.inference.adapters.ollama import ensure_local_offering
from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
from local_model_chat.inference.ledger import InferenceLedger, UsageEvent
from local_model_chat.inference.policy import ControlledGateway, InferencePolicy
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.storage import ChatStore


def _request(request_id: str, message: str = "secret prompt") -> InferenceRequest:
    return InferenceRequest(
        request_id=request_id, project_id=None, offering_id="ollama-local:test:latest",
        model_id="test:latest", purpose=InferencePurpose.CHAT,
        messages=({"role": "user", "content": message},), sensitivity="private",
        required_capabilities=(), max_input_tokens=5, max_output_tokens=5,
        timeout_seconds=60,
    )


def test_ledger_appends_immutable_usage_without_storing_prompt(tmp_path) -> None:
    db = tmp_path / "ledger.sqlite3"
    store = ChatStore(db)
    registry = ProviderRegistry(store)
    ensure_local_offering(registry, "test:latest", context_window=100, max_output_tokens=20)
    ledger = InferenceLedger(store)
    ledger.start(_request("request-1", "super-secret-prompt"))
    ledger.append(UsageEvent("request-1", "started", source_event="gateway"))
    ledger.append(UsageEvent(
        "request-1", "completed", 9, 3, actual_cost_usd=0,
        cost_accuracy="local_zero", source_event="adapter",
        cached_tokens=2, reasoning_tokens=1,
    ))

    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT * FROM inference_requests WHERE request_id = 'request-1'").fetchone()
        assert row is not None
        assert "super-secret-prompt" not in str(row)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == ChatStore.SCHEMA_VERSION
    assert [event["status"] for event in ledger.events("request-1")] == ["started", "completed"]
    assert ledger.events("request-1")[1]["reasoning_tokens"] == 1
    assert ledger.events("request-1")[1]["cached_tokens"] == 2
    with pytest.raises(sqlite3.IntegrityError):
        ledger.start(_request("request-1"))


def test_ledger_attributes_request_to_task_in_same_run(tmp_path) -> None:
    store = ChatStore(tmp_path / "task-ledger.sqlite3")
    registry = ProviderRegistry(store)
    ensure_local_offering(registry, "test:latest", context_window=100, max_output_tokens=20)
    project = store.upsert_project("Example", str(tmp_path), "python")
    run_id = store.create_run(project.id, "Feature", "LOCAL_OFFLINE")
    task_id = store.create_run_task(run_id, "step", "Step", "Edit", "low")
    ledger = InferenceLedger(store)
    ledger.start(replace(_request("task-request"), project_id=project.id,
                         run_id=run_id, task_id=task_id))
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT task_id FROM inference_requests WHERE request_id = 'task-request'"
        ).fetchone()[0] == task_id
    other_run = store.create_run(project.id, "Other", "LOCAL_OFFLINE")
    with pytest.raises(ValueError, match="does not belong"):
        ledger.start(replace(_request("wrong-run"), run_id=other_run, task_id=task_id))


def test_gateway_records_policy_denial_before_adapter_call(tmp_path) -> None:
    store = ChatStore(tmp_path / "ledger.sqlite3")
    registry = ProviderRegistry(store)
    ensure_local_offering(registry, "test:latest", context_window=100, max_output_tokens=20)

    class UncalledAdapter:
        def invoke(self, _request):
            raise AssertionError("adapter must not run")

    gateway = ControlledGateway(InferencePolicy(registry), UncalledAdapter(), InferenceLedger(store))
    with pytest.raises(PermissionError, match="offering status"):
        gateway.invoke(_request("request-2"))
    assert [event["status"] for event in InferenceLedger(store).events("request-2")] == ["denied"]


def test_version_five_upgrade_creates_ledger_and_online_backup(tmp_path) -> None:
    db = tmp_path / "version-five.sqlite3"
    ChatStore(db)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE inference_usage_events")
        connection.execute("DROP TABLE inference_requests")
        connection.execute("PRAGMA user_version = 5")

    ChatStore(db)

    assert len(list(tmp_path.glob("version-five.sqlite3.pre-v15-*.bak"))) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == ChatStore.SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_version_six_upgrade_adds_budget_scopes_without_losing_reservations(tmp_path) -> None:
    db = tmp_path / "version-six.sqlite3"
    store = ChatStore(db)
    project = store.upsert_project("Example", str(tmp_path), "python")
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE budget_scope_limits")
        connection.execute("DROP TABLE budget_reservations")
        connection.execute("DROP TABLE project_budget_limits")
        connection.execute(
            "CREATE TABLE project_budget_limits(project_id INTEGER PRIMARY KEY, limit_usd REAL, updated_at TEXT)"
        )
        connection.execute(
            """CREATE TABLE budget_reservations(
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE, project_id INTEGER,
                amount_usd REAL, actual_usd REAL, status TEXT,
                created_at TEXT, updated_at TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO project_budget_limits VALUES (?, 5, '2026-10-15T00:00:00+00:00')",
            (project.id,),
        )
        connection.execute("PRAGMA user_version = 6")

    ChatStore(db)

    assert len(list(tmp_path.glob("version-six.sqlite3.pre-v15-*.bak"))) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == ChatStore.SCHEMA_VERSION
        assert connection.execute(
            "SELECT limit_usd, premium_reserve_usd FROM project_budget_limits WHERE project_id = ?",
            (project.id,),
        ).fetchone() == (5, 0)
        assert {"task_scope", "tier"} <= {
            row[1] for row in connection.execute("PRAGMA table_info(budget_reservations)")
        }


def test_version_seven_upgrade_adds_reasoning_usage_columns(tmp_path) -> None:
    db = tmp_path / "version-seven.sqlite3"
    ChatStore(db)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE inference_usage_events")
        connection.execute(
            """CREATE TABLE inference_usage_events(
                id INTEGER PRIMARY KEY, request_id TEXT, status TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                estimated_cost_usd REAL, actual_cost_usd REAL,
                cost_accuracy TEXT, source_event TEXT,
                provider_request_id TEXT, created_at TEXT
            )"""
        )
        connection.execute("PRAGMA user_version = 7")

    ChatStore(db)

    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == ChatStore.SCHEMA_VERSION
        assert {"cached_tokens", "reasoning_tokens"} <= {
            row[1] for row in connection.execute("PRAGMA table_info(inference_usage_events)")
        }


def test_run_usage_correlates_local_request_and_opaque_cli_usage(tmp_path) -> None:
    store = ChatStore(tmp_path / "run-usage.sqlite3")
    project = store.upsert_project("Example", str(tmp_path), "python")
    run_id = store.create_run(project.id, "Feature", "HYBRID")
    ensure_local_offering(ProviderRegistry(store), "test:latest", context_window=100, max_output_tokens=20)
    ledger = InferenceLedger(store)
    ledger.start(replace(_request("run-request"), project_id=project.id, run_id=run_id))
    ledger.append(UsageEvent("run-request", "completed", 7, 2, actual_cost_usd=0,
                             cost_accuracy="local_zero", source_event="adapter"))
    store.add_external_call(run_id, "codex", "implementation", "task", 12, 5, 1, "completed")

    usage = store.run_inference_usage(run_id)

    assert usage["local_requests"] == 1
    assert usage["local_input_tokens"] == 7
    assert usage["local_actual_cost_usd"] == 0
    assert usage["opaque_cli_calls"] == 1
    assert usage["opaque_cli_reported_output_tokens"] == 5
    assert usage["opaque_cli_cost_accuracy"] == "unknown"
