from local_model_chat.molicoder.metrics import AdaptiveConcurrency, RunAnalytics
from local_model_chat.storage import ChatStore


def test_adaptive_concurrency_respects_target_cost_and_failure_pressure() -> None:
    policy = AdaptiveConcurrency(max_workers=4)

    assert policy.recommend("python") == 2
    assert policy.recommend("flutter") == 3
    policy.observe(validation_seconds=40, inference_seconds=5, failed=False)
    assert policy.recommend("python") == 3
    policy.observe(validation_seconds=2, inference_seconds=10, failed=True)
    assert policy.recommend("python") == 1


def test_run_analytics_compares_local_and_external_outcomes(tmp_path) -> None:
    store = ChatStore(tmp_path / "metrics.sqlite3")
    project = store.upsert_project("sample", str(tmp_path / "sample"), "python")
    local = store.create_run(project.id, "local", "LOCAL_ONLY")
    store.finish_run(local, "completed")
    external = store.create_run(project.id, "external", "FRONTIER_PLAN")
    store.add_external_call(external, "codex-cli", "planning", "planning_complexity", 100, 20, 2.0, "completed")
    task_id = store.create_run_task(external, "one", "One", "Do one", "low")
    store.finish_run(external, "failed")
    store.add_worker_run(task_id, "executor", "molicoder:latest", "ollama", 50, 10, 1.5, "completed")

    summary = RunAnalytics(store).project_summary(project.id)

    assert summary.total_runs == 2
    assert summary.external_calls == 1
    assert summary.local_worker_runs == 1
    assert summary.local_only_success_rate == 1.0
    assert summary.external_run_success_rate == 0.0
