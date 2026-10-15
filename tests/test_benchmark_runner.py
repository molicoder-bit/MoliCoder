from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.benchmarks.fixtures import load_benchmark_cases
from local_model_chat.benchmarks.runner import BenchmarkCaps, BenchmarkResponse, BenchmarkRunner
from local_model_chat.storage import ChatStore


def test_benchmark_dry_run_is_default_and_respects_call_privacy_caps(tmp_path):
    store = ChatStore(tmp_path / "bench.sqlite3")
    runner = BenchmarkRunner(store)
    calls = []
    caps = BenchmarkCaps("fixture-local", "local", 2, 5000, 100, 60, "private")

    records = runner.run(
        load_benchmark_cases(), caps,
        lambda *_args: calls.append("called"),
    )

    assert calls == []
    assert [item.status for item in records].count("planned") == 2
    assert [item.status for item in records].count("excluded_privacy") == 1
    assert [item.status for item in records].count("capped") == 3


def test_live_benchmark_needs_exact_approval_and_stops_at_cap(tmp_path):
    store = ChatStore(tmp_path / "bench.sqlite3")
    runner = BenchmarkRunner(store)
    caps = BenchmarkCaps("fixture-local", "local", 1, 3000, 100, 60, "private", dry_run=False)
    calls = []
    def invoke(case, maximum):
        calls.append(case.id)
        return BenchmarkResponse("Welcome to local coding.", 20, 8, 0.2, 0.05)

    with pytest.raises(PermissionError):
        runner.run(load_benchmark_cases(), caps, invoke)
    assert calls == []
    store.record_action_authorization(
        project_id=None, action="benchmark", scope=caps.scope(), risk="external",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )

    records = runner.run(load_benchmark_cases(), caps, invoke)

    assert calls == ["chat-greeting"]
    assert records[0].accepted is True and records[0].first_token_seconds == 0.05
    assert records[0].input_tokens == 20 and records[0].output_tokens == 8
    assert all(record.status != "completed" for record in records[1:])


def test_paid_benchmark_requires_spend_cap_and_exact_financial_approval(tmp_path):
    store = ChatStore(tmp_path / "bench.sqlite3")
    runner = BenchmarkRunner(store)
    cases = load_benchmark_cases()
    invoked = []
    no_spend_cap = BenchmarkCaps("fixture-paid", "metered", 1, 3000, 100, 60, "public", dry_run=False)
    with pytest.raises(ValueError, match="spend cap"):
        runner.run(cases, no_spend_cap, lambda *_: invoked.append(True))
    caps = BenchmarkCaps(
        "fixture-paid", "metered", 1, 3000, 100, 60, "public", dry_run=False,
        max_spend_usd=0.01, input_usd_per_million=1.0, output_usd_per_million=2.0,
    )
    with pytest.raises(PermissionError):
        runner.run(cases, caps, lambda *_: invoked.append(True))
    store.record_action_authorization(
        project_id=None, action="benchmark", scope=caps.scope(), risk="financial",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    records = runner.run(cases, caps, lambda case, maximum: (
        invoked.append((case.id, maximum)) or BenchmarkResponse("Welcome!", 20, 8, 0.2)
    ))
    assert invoked == [("chat-greeting", 100)]
    assert records[0].status == "completed"


def test_spend_reservation_can_stop_benchmark_before_provider_call(tmp_path):
    store = ChatStore(tmp_path / "bench.sqlite3")
    runner = BenchmarkRunner(store)
    caps = BenchmarkCaps(
        "fixture-paid", "metered", 1, 3000, 100, 60, "public", dry_run=False,
        max_spend_usd=0.001, input_usd_per_million=1.0, output_usd_per_million=2.0,
    )
    store.record_action_authorization(
        project_id=None, action="benchmark", scope=caps.scope(), risk="financial",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    invoked = []
    records = runner.run(load_benchmark_cases(), caps, lambda *_: invoked.append(True))
    assert invoked == []
    assert records[0].status == "capped"
