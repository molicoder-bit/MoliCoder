import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from local_model_chat.inference.adapters.ollama import ensure_local_offering
from local_model_chat.inference.contracts import InferencePurpose, InferenceRequest
from local_model_chat.inference.ledger import InferenceLedger, UsageEvent
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.observability import TraceExporter
from local_model_chat.storage import ChatStore


def test_trace_export_correlates_usage_without_prompt_or_secret_and_prunes_old_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = ChatStore(tmp_path / "trace.sqlite3")
    project = store.upsert_project("Sample", str(root), "python")
    run_id = store.create_run(project.id, "private request sk-01234567890123456789", "HYBRID")
    store.add_event(run_id, "run_started", {"request": "private prompt", "secret": "credential"})
    store.add_event(run_id, "provider_output", {"content": "private output"})
    store.add_event(run_id, "provider_attempt_started", {
        "key": "task-1", "provider": "codex", "attempt": 1,
        "reason": "api_key=super-secret-value",
    })
    registry = ProviderRegistry(store)
    offering_id = ensure_local_offering(registry, "fixture:latest", context_window=100, max_output_tokens=10)
    ledger = InferenceLedger(store)
    request = InferenceRequest(
        request_id="trace-request-1", project_id=project.id, run_id=run_id,
        offering_id=offering_id, model_id="fixture:latest", purpose=InferencePurpose.REVIEW,
        messages=({"role": "user", "content": "private prompt api_key=super-secret-value"},),
        sensitivity="private", required_capabilities=(),
        max_input_tokens=10, max_output_tokens=5, timeout_seconds=60,
    )
    ledger.start(request)
    ledger.append(UsageEvent("trace-request-1", "completed", 4, 2,
                             actual_cost_usd=0, cost_accuracy="local_zero"))
    output_dir = root / ".molicoder" / "trace-exports"
    output_dir.mkdir(parents=True)
    old = output_dir / "trace-0-old.jsonl"
    old.write_text("old\n", encoding="utf-8")
    old_time = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
    os.utime(old, (old_time, old_time))

    path = TraceExporter(store).export_run(run_id)
    content = path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in content.splitlines()]
    assert not old.exists()
    assert any(record["kind"] == "provider_attempt_started" for record in records if record["type"] == "run_event")
    assert any(record["request_id"] == "trace-request-1" and record["run_id"] == run_id
               and record["purpose"] == "review" for record in records if record["type"] == "inference_usage")
    for secret in ("private prompt", "private output", "super-secret-value", "credential", "sk-01234567890123456789"):
        assert secret not in content


def test_trace_export_rejects_old_events_and_invalid_retention(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = ChatStore(tmp_path / "trace.sqlite3")
    project = store.upsert_project("Sample", str(root), "python")
    run_id = store.create_run(project.id, "request", "HYBRID")
    event_id = store.add_event(run_id, "task_completed", {"key": "old-task"})
    with store._connect() as connection:
        connection.execute("UPDATE events SET created_at = ? WHERE id = ?", (
            (datetime.now() - timedelta(days=40)).isoformat(), event_id,
        ))
        connection.commit()
    path = TraceExporter(store).export_run(run_id)
    assert path.read_text(encoding="utf-8") == ""
    with pytest.raises(ValueError):
        TraceExporter(store, retention_days=0)
