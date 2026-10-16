"""End-to-end execution checks against isolated, temporary Git projects."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult
from local_model_chat.molicoder.runtime import build_validator
from local_model_chat.molicoder.validate import ProjectValidator, ValidationCommand
from test_build_plan_approval import approve, fixture, git


class OneTaskPlanner:
    provider = "scripted"

    def __init__(self, path: str):
        self.path = path

    def plan(self, _packet):
        return parse_plan({"objective": "Verify a code change", "tasks": [{
            "key": "change", "title": "Change code", "objective": f"Update {self.path}",
            "risk": "low", "depends_on": [], "expected_paths": [self.path],
            "acceptance_criteria": ["Configured validation must pass"],
            "verification_checks": ["project-default"],
        }]})


class WritingProvider:
    name = "scripted"
    model = "fixture-model"

    def __init__(self, writes: dict[str, str]):
        self.writes = writes
        self.calls = 0

    def execute(self, worktree, _prompt, *, timeout_seconds=1200):
        self.calls += 1
        for name, content in self.writes.items():
            path = Path(worktree) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return ProviderResult("completed", "Done", seconds=0.01)

    def cancel(self):
        pass


def _check_unittest(root: Path, module: str) -> str:
    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-q", module], cwd=root,
        capture_output=True, text=True, timeout=15,
    )
    return "validation passed" if result.returncode == 0 else "validation failed: " + result.stderr


def _run(tmp_path, path: str, writes: dict[str, str], validate):
    root, store, _project, _old_provider, orchestrator = fixture(tmp_path)
    orchestrator.planner = OneTaskPlanner(path)
    provider = WritingProvider(writes)
    orchestrator.provider = provider
    orchestrator.validate = validate
    orchestrator.validate_task = lambda worktree, _task: validate(worktree)
    orchestrator.max_task_attempts = 1
    return root, store, provider, orchestrator


def _execute(store, orchestrator):
    prepared = orchestrator.run("Verify the code change", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"
    approve(store, prepared.run_id)
    return prepared.run_id, orchestrator.execute_approved(prepared.run_id)


def test_s051_implement_arithmetic_against_independent_tests(tmp_path):
    root, store, provider, orchestrator = _run(
        tmp_path, "calc.py", {"calc.py": "def add(a, b):\n    return a + b\n"},
        lambda path: _check_unittest(path, "test_calc"),
    )
    (root / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class CalculationTest(unittest.TestCase):\n"
        "    def test_values(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
        "        self.assertEqual(add(-4, 1), -3)\n"
        "        self.assertEqual(add(0, 0), 0)\n", encoding="utf-8",
    )
    git(root, "add", "test_calc.py")
    git(root, "commit", "-m", "independent arithmetic tests")
    run_id, result = _execute(store, orchestrator)
    assert result.status == "ready_to_apply", result.error
    assert provider.calls == 1
    assert _check_unittest(result.integration_path, "test_calc") == "validation passed"
    assert store.list_run_tasks(run_id)[0].status == "completed"
    assert not (root / "calc.py").exists()


def test_s052_fix_incorrect_function_and_pass_existing_tests(tmp_path):
    root, store, provider, orchestrator = _run(
        tmp_path, "calc.py", {"calc.py": "def add(a, b):\n    return a + b\n"},
        lambda path: _check_unittest(path, "test_calc"),
    )
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class CalculationTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "incorrect function and failing test")
    assert _check_unittest(root, "test_calc").startswith("validation failed")
    _, result = _execute(store, orchestrator)
    assert result.status == "ready_to_apply", result.error
    assert provider.calls == 1
    assert _check_unittest(result.integration_path, "test_calc") == "validation passed"
    assert (root / "calc.py").read_text(encoding="utf-8").endswith("a - b\n")


def test_s053_added_unit_test_runs_and_rejects_mutant(tmp_path):
    test_source = (
        "import unittest\nfrom calc import add\n"
        "class CalculationTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )
    root, store, provider, orchestrator = _run(
        tmp_path, "test_calc.py", {"test_calc.py": test_source},
        lambda path: _check_unittest(path, "test_calc"),
    )
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(root, "add", "calc.py")
    git(root, "commit", "-m", "arithmetic implementation")
    _, result = _execute(store, orchestrator)
    assert result.status == "ready_to_apply", result.error
    assert provider.calls == 1
    assert _check_unittest(result.integration_path, "test_calc") == "validation passed"
    mutant = tmp_path / "mutant"
    mutant.mkdir()
    shutil.copy2(result.integration_path / "test_calc.py", mutant / "test_calc.py")
    (mutant / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    assert _check_unittest(mutant, "test_calc").startswith("validation failed")
    assert not (root / "test_calc.py").exists()


def test_s054_syntax_error_fails_verification(tmp_path):
    def validate(path):
        try:
            ast.parse((Path(path) / "calc.py").read_text(encoding="utf-8"))
        except SyntaxError as exc:
            return f"validation failed: syntax error: {exc.msg}"
        return "validation passed"

    root, store, provider, orchestrator = _run(
        tmp_path, "calc.py", {"calc.py": "def add(:\n    pass\n"}, validate,
    )
    _, result = _execute(store, orchestrator)
    assert result.status == "needs_attention"
    assert "syntax error" in result.error
    assert provider.calls == 1
    assert not (root / "calc.py").exists()


def test_s055_type_error_fails_configured_static_check(tmp_path):
    root, store, provider, orchestrator = _run(
        tmp_path, "typed.py", {"typed.py": "def length(text: str) -> int:\n    return text\n"},
        lambda _path: "validation passed",
    )
    config_dir = root / ".molicoder"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "project.json").write_text(json.dumps({
        "version": 1, "kind": "python", "constraints": [],
        "validation": [{
            "name": "mypy", "kind": "type", "timeout_seconds": 30,
            "argv": ["{python}", "-m", "mypy", "--strict", "--cache-dir", ".molicoder/mypy-cache", "typed.py"],
        }],
    }), encoding="utf-8")
    validate = lambda path: build_validator(path, environment_root=root).run_and_summarize()
    orchestrator.validate = validate
    orchestrator.validate_task = lambda path, _task: validate(path)
    run_id, result = _execute(store, orchestrator)
    assert result.status == "needs_attention"
    assert "type/mypy" in result.error
    assert "Incompatible return value type" in result.error
    assert provider.calls == 1
    assert store.list_run_tasks(run_id)[0].status == "needs_attention"
    assert not (root / "typed.py").exists()


def test_s056_success_text_without_change_is_not_completion(tmp_path):
    root, store, provider, orchestrator = _run(
        tmp_path, "calc.py", {}, lambda _path: "validation passed",
    )
    run_id, result = _execute(store, orchestrator)
    assert result.status == "needs_attention"
    assert "without making a repository change" in result.error
    assert store.list_run_tasks(run_id)[0].status == "needs_attention"
    assert provider.calls == 1 and not (root / "calc.py").exists()


def test_s057_out_of_scope_file_is_rejected(tmp_path):
    root, store, provider, orchestrator = _run(
        tmp_path, "calc.py",
        {"calc.py": "def add(a, b):\n    return a + b\n", "unapproved.txt": "extra\n"},
        lambda _path: "validation passed",
    )
    run_id, result = _execute(store, orchestrator)
    assert result.status == "needs_attention"
    assert "outside the approved task scope: unapproved.txt" in result.error
    assert store.list_run_tasks(run_id)[0].status == "needs_attention"
    assert provider.calls == 1
    assert not (root / "calc.py").exists() and not (root / "unapproved.txt").exists()


def test_s058_long_validation_reports_progress_times_out_and_cancels(tmp_path):
    timeout_root = tmp_path / "timeout-project"
    timeout_root.mkdir()
    sleeper = ValidationCommand(
        "slow-test", (sys.executable, "-c", "import time; time.sleep(30)"),
        timeout_seconds=0.5,
    )
    progress = []
    started_at = time.monotonic()
    timed = ProjectValidator(
        timeout_root, [sleeper], on_progress=lambda command, elapsed: progress.append((command.name, elapsed)),
    ).run()
    assert not timed.passed and timed.results[0].exit_code == 124
    assert "validation timed out" in timed.results[0].log_path.read_text(encoding="utf-8")
    assert progress and progress[0][0] == "slow-test"
    assert time.monotonic() - started_at < 10

    root, store, _project, _old_provider, orchestrator = fixture(tmp_path)
    orchestrator.planner = OneTaskPlanner("calc.py")
    orchestrator.provider = WritingProvider({"calc.py": "def add(a, b):\n    return a + b\n"})
    cancelled = threading.Event()
    validating = threading.Event()
    orchestrator.should_cancel = cancelled.is_set

    def validate_task(worktree, _task):
        return ProjectValidator(
            worktree, [ValidationCommand("slow-test", sleeper.argv, timeout_seconds=30)],
            should_cancel=cancelled.is_set,
            on_progress=lambda _command, _elapsed: validating.set(),
        ).run_and_summarize()

    orchestrator.validate_task = validate_task
    orchestrator.validate = lambda _path: "validation passed"
    prepared = orchestrator.run("Implement calc.py", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"
    approve(store, prepared.run_id)
    results = []
    worker = threading.Thread(target=lambda: results.append(orchestrator.execute_approved(prepared.run_id)))
    worker.start()
    try:
        assert validating.wait(5), "validation did not start"
        cancelled.set()
        orchestrator.cancel()
    finally:
        cancelled.set()
        worker.join(timeout=5)
    assert not worker.is_alive() and len(results) == 1
    result = results[0]
    assert result.status == "cancelled"
    assert store.list_run_tasks(prepared.run_id)[0].status == "cancelled"
    assert not (root / "calc.py").exists()
    assert (result.integration_path / "calc.py").is_file()


def test_s059_cancel_running_task_keeps_partial_work_isolated(tmp_path):
    root, store, _project, _old_provider, orchestrator = fixture(tmp_path)
    orchestrator.planner = OneTaskPlanner("calc.py")

    class BlockingProvider:
        name = "scripted"
        model = "fixture-model"

        def __init__(self):
            self.started = threading.Event()
            self.stopped = threading.Event()
            self.calls = 0

        def execute(self, worktree, _prompt, *, timeout_seconds=1200):
            self.calls += 1
            (Path(worktree) / "calc.py").write_text("partial work\n", encoding="utf-8")
            self.started.set()
            if not self.stopped.wait(5):
                raise AssertionError("cancellation did not reach the active provider")
            return ProviderResult("cancelled", "cancelled by user", seconds=0.01)

        def cancel(self):
            self.stopped.set()

    provider = BlockingProvider()
    orchestrator.provider = provider
    orchestrator.validate = lambda _path: "validation passed"
    orchestrator.validate_task = lambda _path, _task: "validation passed"
    prepared = orchestrator.run("Implement calc.py", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"
    approve(store, prepared.run_id)
    results = []
    worker = threading.Thread(target=lambda: results.append(orchestrator.execute_approved(prepared.run_id)))
    worker.start()
    try:
        assert provider.started.wait(5), "provider did not start"
        orchestrator.cancel()
    finally:
        provider.stopped.set()
        worker.join(timeout=5)
    assert not worker.is_alive() and len(results) == 1
    result = results[0]
    assert result.status == "cancelled"
    assert store.get_run(prepared.run_id)["status"] == "cancelled"
    assert store.list_run_tasks(prepared.run_id)[0].status == "cancelled"
    assert store.get_task_completion(store.list_run_tasks(prepared.run_id)[0].id) is None
    assert (result.integration_path / "calc.py").read_text(encoding="utf-8") == "partial work\n"
    assert not (root / "calc.py").exists()
    assert provider.calls == 1


def test_s060_final_project_regression_blocks_apply(tmp_path):
    root, store, provider, orchestrator = _run(
        tmp_path, "calc.py", {"calc.py": "def value():\n    return 2\n"},
        lambda path: "validation passed" if "return 2" in (Path(path) / "calc.py").read_text(encoding="utf-8")
        else "validation failed: task output differs",
    )
    (root / "calc.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "test_regression.py").write_text(
        "import unittest\nfrom calc import value\n"
        "class RegressionTest(unittest.TestCase):\n"
        "    def test_existing_contract(self):\n        self.assertEqual(value(), 1)\n",
        encoding="utf-8",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "existing project contract")
    assert _check_unittest(root, "test_regression") == "validation passed"
    orchestrator.validate = lambda path: _check_unittest(path, "test_regression")
    run_id, result = _execute(store, orchestrator)
    assert result.status == "needs_attention"
    assert "validation failed" in result.error
    assert provider.calls == 1
    assert store.list_run_tasks(run_id)[0].status == "completed"
    assert _check_unittest(result.integration_path, "test_regression").startswith("validation failed")
    assert (root / "calc.py").read_text(encoding="utf-8").endswith("return 1\n")
