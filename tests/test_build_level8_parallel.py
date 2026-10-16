"""Approval-gated parallel worktree execution against real temporary Git repositories."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from local_model_chat.molicoder.executor import ExecutionResult
from local_model_chat.molicoder.orchestrator import WorktreeOrchestrator
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.repair import RepairOutcome
from test_build_plan_approval import approve, fixture


def _task(key, *parents, path=None):
    return {
        "key": key, "title": key, "objective": f"Write {path or key + '.txt'}",
        "risk": "low", "depends_on": list(parents), "expected_paths": [path or f"{key}.txt"],
    }


def _setup(tmp_path, tasks, *, max_workers=2, validate_task=None, validate_integration=None):
    root, store, project, _provider, _old_orchestrator = fixture(tmp_path)
    plan = parse_plan({"objective": "Build a dependency graph", "tasks": tasks}, max_tasks=20)

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return plan

    expected = {task["key"] for task in tasks}
    validate_task = validate_task or (lambda path, task: (
        "validation passed" if (Path(path) / f"{task.key}.txt").is_file()
        and (Path(path) / f"{task.key}.txt").read_text(encoding="utf-8") == f"{task.key}\n"
        else "validation failed"
    ))
    validate_integration = validate_integration or (lambda path: (
        "validation passed" if all(
            (Path(path) / f"{key}.txt").is_file()
            and (Path(path) / f"{key}.txt").read_text(encoding="utf-8") == f"{key}\n"
            for key in expected
        ) else "validation failed"
    ))
    orchestrator = WorktreeOrchestrator(
        store=store, project=project, planner=Planner(),
        execute_task=lambda *_args: (_ for _ in ()).throw(AssertionError("executor not installed")),
        validate_task=validate_task, validate_integration=validate_integration,
        worktree_base=tmp_path / "parallel-trees", max_workers=max_workers,
        require_plan_approval=True,
    )
    return root, store, orchestrator


def _completed():
    execution = ExecutionResult("completed", "done", 1, 1)
    return RepairOutcome("completed", 1, execution, (), "", (execution,))


def _execute(store, orchestrator):
    prepared = orchestrator.run("Build a dependency graph")
    assert prepared.status == "awaiting_approval"
    assert store.list_run_tasks(prepared.run_id) == []
    with pytest.raises(PermissionError):
        orchestrator.execute_approved(prepared.run_id)
    approve(store, prepared.run_id)
    return orchestrator.execute_approved(prepared.run_id)


def test_s022_config_creation_precedes_importing_code(tmp_path):
    tasks = [
        {**_task("config", path="config.py"), "objective": "Create config.py with VALUE"},
        {**_task("importer", "config", path="feature.py"), "objective": "Import VALUE from config.py"},
    ]
    root, store, orchestrator = _setup(
        tmp_path, tasks, max_workers=2,
        validate_task=lambda path, task: "validation passed" if (
            (Path(path) / task.expected_paths[0]).is_file()
        ) else "validation failed",
        validate_integration=lambda path: "validation passed" if (
            (Path(path) / "config.py").read_text(encoding="utf-8") == "VALUE = 42\n"
            and (Path(path) / "feature.py").read_text(encoding="utf-8") == "from config import VALUE\n"
        ) else "validation failed",
    )
    order = []

    def execute(worktree, prompt):
        path = Path(worktree)
        if "(config)" in prompt:
            order.append("config")
            (path / "config.py").write_text("VALUE = 42\n", encoding="utf-8")
        else:
            assert (path / "config.py").read_text(encoding="utf-8") == "VALUE = 42\n"
            order.append("importer")
            (path / "feature.py").write_text("from config import VALUE\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    prepared = orchestrator.run("Create a config file, then write code that imports it")
    assert prepared.status == "awaiting_approval"
    planned = store.get_latest_plan_record(prepared.run_id)["payload"]["tasks"]
    assert [(task["key"], task["depends_on"]) for task in planned] == [
        ("config", []), ("importer", ["config"]),
    ]
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "completed", result.error
    assert order == ["config", "importer"]
    assert not (root / "feature.py").exists()


def test_s024_three_step_chain_waits_for_each_verified_parent(tmp_path):
    tasks = [_task("a"), _task("b", "a"), _task("c", "b")]
    root, store, orchestrator = _setup(tmp_path, tasks, max_workers=3)
    order = []

    def execute(worktree, prompt):
        key = next(task["key"] for task in tasks if f"({task['key']})" in prompt)
        path = Path(worktree)
        if key != "a":
            parent = "a" if key == "b" else "b"
            assert (path / f"{parent}.txt").read_text(encoding="utf-8") == f"{parent}\n"
            saved = {task.key: task for task in store.list_run_tasks(run_id)}
            assert store.get_task_completion(saved[parent].id) is not None
        order.append(key)
        (path / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    prepared = orchestrator.run("Complete A, then B, then C")
    run_id = prepared.run_id
    approve(store, run_id)
    result = orchestrator.execute_approved(run_id)
    assert result.status == "completed", result.error
    assert order == ["a", "b", "c"]
    assert all((result.integration_path / f"{key}.txt").is_file() for key in order)
    assert not (root / "c.txt").exists()


def test_s027_explicit_user_order_is_recorded_and_enforced(tmp_path):
    root, store, orchestrator = _setup(tmp_path, [_task("x"), _task("y", "x")], max_workers=2)
    order = []

    def execute(worktree, prompt):
        key = "x" if "(x)" in prompt else "y"
        if key == "y":
            assert (Path(worktree) / "x.txt").is_file()
        order.append(key)
        (Path(worktree) / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    prepared = orchestrator.run("Do X before Y")
    payload = store.get_latest_plan_record(prepared.run_id)["payload"]
    assert payload["tasks"][1]["depends_on"] == ["x"]
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "completed", result.error
    assert order == ["x", "y"]
    assert not (root / "y.txt").exists()


def test_s081_s086_independent_tasks_overlap_and_integrate_distinct_worktrees(tmp_path):
    root, store, orchestrator = _setup(tmp_path, [_task("alpha"), _task("beta")])
    barrier = threading.Barrier(2, timeout=20)
    worktrees = {}

    def execute(worktree, prompt):
        key = "alpha" if "(alpha)" in prompt else "beta"
        worktrees[key] = Path(worktree)
        barrier.wait()
        (Path(worktree) / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    result = _execute(store, orchestrator)
    assert result.status == "completed", result.error
    planned = store.get_latest_plan_record(result.run_id)["payload"]["tasks"]
    assert [task["depends_on"] for task in planned] == [[], []]
    assert worktrees["alpha"] != worktrees["beta"]
    assert all((result.integration_path / f"{key}.txt").read_text(encoding="utf-8") == f"{key}\n"
               for key in ("alpha", "beta"))
    assert not (root / "alpha.txt").exists() and not (root / "beta.txt").exists()


def test_s083_s087_diamond_waits_for_verified_parent_artifacts(tmp_path):
    tasks = [_task("a"), _task("b", "a"), _task("c", "a"), _task("d", "b", "c")]
    root, store, orchestrator = _setup(tmp_path, tasks)
    calls = []
    middle = threading.Barrier(2, timeout=20)

    def execute(worktree, prompt):
        key = next(task["key"] for task in tasks if f"({task['key']})" in prompt)
        path = Path(worktree)
        if key in {"b", "c"}:
            assert (path / "a.txt").read_text(encoding="utf-8") == "a\n"
            middle.wait()
        if key == "d":
            assert all((path / f"{parent}.txt").read_text(encoding="utf-8") == f"{parent}\n"
                       for parent in ("a", "b", "c"))
            assert all(store.get_task_completion(task.id) is not None
                       for task in store.list_run_tasks(orchestrator_run_id)
                       if task.key in {"a", "b", "c"})
        calls.append(key)
        (path / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    # The run ID is read only after plan creation, before approval and workers.
    prepared = orchestrator.run("Build a dependency graph")
    assert prepared.status == "awaiting_approval"
    planned = store.get_latest_plan_record(prepared.run_id)["payload"]["tasks"]
    assert [task["depends_on"] for task in planned] == [[], ["a"], ["a"], ["b", "c"]]
    orchestrator_run_id = prepared.run_id
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "completed", result.error
    assert calls[0] == "a" and calls[-1] == "d" and set(calls[1:3]) == {"b", "c"}
    assert all((result.integration_path / f"{key}.txt").is_file() for key in ("a", "b", "c", "d"))
    assert not (root / "d.txt").exists()


def test_s084_failed_parent_blocks_child_while_unrelated_sibling_finishes(tmp_path):
    root, store, orchestrator = _setup(tmp_path, [_task("parent"), _task("independent"), _task("child", "parent")])
    barrier = threading.Barrier(2, timeout=20)
    calls = []

    def execute(worktree, prompt):
        key = next(key for key in ("parent", "independent", "child") if f"({key})" in prompt)
        calls.append(key)
        barrier.wait()
        if key == "parent":
            execution = ExecutionResult("failed", "", 1, 0, "deliberate failure")
            return RepairOutcome("failed", 1, execution, ("failure",), "validation failed: parent", (execution,))
        (Path(worktree) / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    result = _execute(store, orchestrator)
    assert result.status == "failed"
    statuses = {task.key: task.status for task in store.list_run_tasks(result.run_id)}
    assert statuses == {"parent": "failed", "independent": "completed", "child": "proposed"}
    assert calls == ["parent", "independent"] or calls == ["independent", "parent"]
    assert not (root / "independent.txt").exists()


def test_s085_conflicting_same_file_edits_do_not_silently_overwrite(tmp_path):
    tasks = [_task("first", path="shared.txt"), _task("second", path="shared.txt")]
    root, store, orchestrator = _setup(
        tmp_path, tasks,
        validate_task=lambda path, task: (
            "validation passed" if (Path(path) / "shared.txt").read_text(encoding="utf-8") == f"{task.key}\n"
            else "validation failed"
        ),
        validate_integration=lambda path: (
            "validation passed" if (Path(path) / "shared.txt").read_text(encoding="utf-8") == "first\nsecond\n"
            else "validation failed"
        ),
    )
    (root / "shared.txt").write_text("base\n", encoding="utf-8")
    from test_build_plan_approval import git
    git(root, "add", "shared.txt")
    git(root, "commit", "-m", "shared base")
    events = []
    orchestrator.on_event = lambda kind, payload: events.append((kind, payload))

    def execute(worktree, prompt):
        key = "first" if "(first)" in prompt else "second"
        (Path(worktree) / "shared.txt").write_text(f"{key}\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    result = _execute(store, orchestrator)
    assert result.status == "failed"
    assert any(kind == "integration_conflict" and "shared.txt" in payload["paths"]
               for kind, payload in events)
    assert (root / "shared.txt").read_text(encoding="utf-8") == "base\n"


def test_s088_s089_ready_child_starts_while_slow_branch_remains_running(tmp_path):
    root, store, orchestrator = _setup(tmp_path, [_task("fast"), _task("slow"), _task("child", "fast")])
    slow_started = threading.Event()
    release_slow = threading.Event()
    observed = {}
    calls = []

    def execute(worktree, prompt):
        key = next(key for key in ("fast", "slow", "child") if f"({key})" in prompt)
        calls.append(key)
        if key == "slow":
            slow_started.set()
            assert release_slow.wait(20)
        elif key == "fast":
            assert slow_started.wait(20)
        else:
            statuses = {task.key: task.status for task in store.list_run_tasks(run_id)}
            observed.update(statuses)
            assert (Path(worktree) / "fast.txt").read_text(encoding="utf-8") == "fast\n"
            release_slow.set()
        (Path(worktree) / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    prepared = orchestrator.run("Build a dependency graph")
    run_id = prepared.run_id
    assert prepared.status == "awaiting_approval"
    approve(store, run_id)
    result = orchestrator.execute_approved(run_id)
    assert result.status == "completed", result.error
    assert observed["fast"] == "completed" and observed["slow"] == "running"
    assert calls.count("fast") == 1 and calls.count("child") == 1 and calls.count("slow") == 1
    assert all((result.integration_path / f"{key}.txt").is_file() for key in ("fast", "slow", "child"))
    assert not (root / "child.txt").exists()


def test_s090_twenty_task_mixed_graph_runs_each_task_once_after_prerequisites(tmp_path):
    keys = [f"t{index:02}" for index in range(20)]
    tasks = [_task(key, keys[index - 4]) if index >= 4 else _task(key)
             for index, key in enumerate(keys)]
    root, store, orchestrator = _setup(tmp_path, tasks, max_workers=4)
    calls = []
    lock = threading.Lock()

    def execute(worktree, prompt):
        key = next(key for key in keys if f"({key})" in prompt)
        index = keys.index(key)
        if index >= 4:
            parent = keys[index - 4]
            assert (Path(worktree) / f"{parent}.txt").read_text(encoding="utf-8") == f"{parent}\n"
        with lock:
            calls.append(key)
        (Path(worktree) / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    result = _execute(store, orchestrator)
    assert result.status == "completed", result.error
    assert len(calls) == len(set(calls)) == 20
    assert all((result.integration_path / f"{key}.txt").is_file() for key in keys)
    assert all(task.status == "completed" for task in store.list_run_tasks(result.run_id))
    assert not any((root / f"{key}.txt").exists() for key in keys)


def test_approved_parallel_task_cannot_integrate_out_of_scope_file(tmp_path):
    root, store, orchestrator = _setup(tmp_path, [_task("expected")], max_workers=1)

    def execute(worktree, _prompt):
        (Path(worktree) / "expected.txt").write_text("expected\n", encoding="utf-8")
        (Path(worktree) / "unapproved.txt").write_text("unapproved\n", encoding="utf-8")
        return _completed()

    orchestrator.execute_task = execute
    result = _execute(store, orchestrator)
    assert result.status == "failed" and "outside its approved file scope" in result.error
    assert not (root / "expected.txt").exists() and not (root / "unapproved.txt").exists()
