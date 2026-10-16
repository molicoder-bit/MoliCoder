"""Evidence-driven, bounded repair in the approved sequential Build path."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult, ProviderStatus
from test_build_level5_execution import _check_unittest, _execute, _run
from test_build_plan_approval import approve, fixture, git


class SequenceProvider:
    name = "scripted"
    model = "fixture-model"

    def __init__(self, versions: list[str]):
        self.versions = versions
        self.prompts: list[str] = []

    def execute(self, worktree, prompt, *, timeout_seconds=1200):
        self.prompts.append(prompt)
        (Path(worktree) / "calc.py").write_text(
            self.versions[min(len(self.prompts) - 1, len(self.versions) - 1)], encoding="utf-8"
        )
        return ProviderResult("completed", "Done", seconds=0.01)

    def cancel(self):
        pass


def _repair_fixture(tmp_path, versions: list[str], test_source: str, *, max_attempts: int = 2):
    root, store, _provider, orchestrator = _run(
        tmp_path, "calc.py", {}, lambda path: _check_unittest(path, "test_calc"),
    )
    (root / "test_calc.py").write_text(test_source, encoding="utf-8")
    git(root, "add", "test_calc.py")
    git(root, "commit", "-m", "independent acceptance test")
    provider = SequenceProvider(versions)
    orchestrator.provider = provider
    orchestrator.max_task_attempts = max_attempts
    events = []
    orchestrator.on_event = lambda kind, payload: events.append((kind, payload))
    return root, store, provider, orchestrator, events


ADD_TEST = (
    "import unittest\nfrom calc import add\n"
    "class CalculationTest(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)
CORRECT_ADD = "def add(a, b):\n    return a + b\n"


def test_s061_one_failed_implementation_then_verified_repair(tmp_path):
    root, store, provider, orchestrator, events = _repair_fixture(
        tmp_path, ["def add(a, b):\n    return a - b\n", CORRECT_ADD], ADD_TEST,
    )
    run_id, result = _execute(store, orchestrator)
    assert result.status == "ready_to_apply", result.error
    assert len(provider.prompts) == 2
    assert "validation failed" in provider.prompts[1]
    assert any(kind == "repair_started" and payload["attempt"] == 2 for kind, payload in events)
    assert _check_unittest(result.integration_path, "test_calc") == "validation passed"
    assert store.list_run_tasks(run_id)[0].attempts == 2
    assert not (root / "calc.py").exists()


def test_s062_second_bad_attempt_is_rejected_before_third_repair(tmp_path):
    _root, store, provider, orchestrator, events = _repair_fixture(
        tmp_path,
        ["def add(a, b):\n    return a - b\n",
         "def add(a, b):\n    return a * b\n", CORRECT_ADD],
        ADD_TEST, max_attempts=3,
    )
    _, result = _execute(store, orchestrator)
    assert result.status == "ready_to_apply", result.error
    assert len(provider.prompts) == 3
    assert "validation failed" in provider.prompts[1]
    assert "validation failed" in provider.prompts[2]
    assert len([1 for kind, _ in events if kind == "repair_started"]) == 2
    assert _check_unittest(result.integration_path, "test_calc") == "validation passed"


def test_s063_identical_validation_failures_stop_without_fourth_attempt(tmp_path):
    _root, store, provider, orchestrator, events = _repair_fixture(
        tmp_path, ["def add(a, b):\n    return a - b\n"] * 4,
        ADD_TEST, max_attempts=4,
    )
    _, result = _execute(store, orchestrator)
    assert result.status == "needs_attention"
    assert "repair stuck after repeated validation failure" in result.error
    assert len(provider.prompts) == 3
    assert any(kind == "repair_stopped_no_progress" and payload["pattern"] == "stuck"
               for kind, payload in events)


def test_s064_missing_import_is_repaired_with_test_evidence(tmp_path):
    test_source = (
        "import unittest\nfrom calc import square_root\n"
        "class CalculationTest(unittest.TestCase):\n"
        "    def test_square_root(self):\n        self.assertEqual(square_root(9), 3)\n"
    )
    _root, store, provider, orchestrator, _events = _repair_fixture(
        tmp_path,
        ["def square_root(value):\n    return math.sqrt(value)\n",
         "import math\ndef square_root(value):\n    return math.sqrt(value)\n"],
        test_source,
    )
    _, result = _execute(store, orchestrator)
    assert result.status == "ready_to_apply", result.error
    assert len(provider.prompts) == 2 and "NameError" in provider.prompts[1]
    assert _check_unittest(result.integration_path, "test_calc") == "validation passed"


def test_s065_wrong_return_type_is_corrected(tmp_path):
    test_source = (
        "import unittest\nfrom calc import add\n"
        "class CalculationTest(unittest.TestCase):\n"
        "    def test_integer_result(self):\n"
        "        result = add(2, 3)\n"
        "        self.assertIsInstance(result, int)\n"
        "        self.assertEqual(result, 5)\n"
    )
    _root, store, provider, orchestrator, _events = _repair_fixture(
        tmp_path, ["def add(a, b):\n    return str(a + b)\n", CORRECT_ADD], test_source,
    )
    _, result = _execute(store, orchestrator)
    assert result.status == "ready_to_apply", result.error
    assert len(provider.prompts) == 2 and "AssertionError" in provider.prompts[1]
    assert _check_unittest(result.integration_path, "test_calc") == "validation passed"


def test_s066_regression_from_repair_requires_third_verified_attempt(tmp_path):
    test_source = (
        "import unittest\nfrom calc import add, subtract\n"
        "class CalculationTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
        "    def test_existing_subtract(self):\n        self.assertEqual(subtract(2, 3), -1)\n"
    )
    versions = [
        "def add(a, b):\n    return a - b\ndef subtract(a, b):\n    return a - b\n",
        "def add(a, b):\n    return a + b\ndef subtract(a, b):\n    return a + b\n",
        "def add(a, b):\n    return a + b\ndef subtract(a, b):\n    return a - b\n",
    ]
    _root, store, provider, orchestrator, _events = _repair_fixture(
        tmp_path, versions, test_source, max_attempts=3,
    )
    _, result = _execute(store, orchestrator)
    assert result.status == "ready_to_apply", result.error
    assert len(provider.prompts) == 3
    assert "test_existing_subtract" in provider.prompts[2]
    assert _check_unittest(result.integration_path, "test_calc") == "validation passed"


def test_s067_broken_oracle_is_not_changed_to_make_bad_result_pass(tmp_path):
    impossible_test = (
        "import unittest\nfrom calc import add\n"
        "class CalculationTest(unittest.TestCase):\n"
        "    def test_wrong_expectation(self):\n        self.assertEqual(add(2, 3), 6)\n"
    )
    root, store, provider, orchestrator, _events = _repair_fixture(
        tmp_path, [CORRECT_ADD, CORRECT_ADD], impossible_test,
    )
    _, result = _execute(store, orchestrator)
    assert result.status == "needs_attention"
    assert "validation failed" in result.error
    assert len(provider.prompts) == 2
    assert (root / "test_calc.py").read_text(encoding="utf-8") == impossible_test
    assert (result.integration_path / "test_calc.py").read_text(encoding="utf-8") == impossible_test
    assert not (root / "calc.py").exists()


def test_s068_transient_outage_is_classified_and_uses_available_route(tmp_path):
    root, store, _project, _provider, orchestrator = fixture(tmp_path, "hello.txt", "hello\n")
    events = []
    orchestrator.on_event = lambda kind, payload: events.append((kind, payload))
    orchestrator.max_task_attempts = 2

    class OutageProvider:
        name = "primary"
        model = "fixture-primary"
        calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return ProviderResult("needs_attention", "", error="service unavailable")

        def cancel(self):
            pass

    class AvailableProvider:
        name = "fallback"
        model = "fixture-fallback"
        calls = 0

        def probe(self):
            return ProviderStatus(self.name, True, True, auth_verified=True)

        def execute(self, worktree, _prompt, **_kwargs):
            self.calls += 1
            (Path(worktree) / "hello.txt").write_text("hello\n", encoding="utf-8")
            return ProviderResult("completed", "Done")

        def cancel(self):
            pass

    primary, fallback = OutageProvider(), AvailableProvider()
    orchestrator.provider = primary
    orchestrator.alternate_provider = fallback
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert primary.calls == fallback.calls == 1
    assert any(kind == "provider_failure_action" and payload["category"] == "outage"
               for kind, payload in events)
    assert any(kind == "model_handoff" and payload["provider"] == "fallback"
               for kind, payload in events)
    assert not any(kind == "repair_started" for kind, _ in events)
    assert not (root / "hello.txt").exists()


def test_s069_scope_expansion_reapproves_and_preserves_completed_checkpoint(tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from local_model_chat.ui.main_window import MainWindow

    root, store, project, _provider, orchestrator = fixture(tmp_path)
    (root / "test_calc.py").write_text(ADD_TEST, encoding="utf-8")
    git(root, "add", "test_calc.py")
    git(root, "commit", "-m", "independent acceptance test")

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Build and repair", "tasks": [
                {"key": "first", "title": "First", "objective": "Write first.txt",
                 "risk": "low", "depends_on": [], "expected_paths": ["first.txt"]},
                {"key": "calc", "title": "Calculation", "objective": "Implement calc.py",
                 "risk": "low", "depends_on": ["first"], "expected_paths": ["calc.py"]},
            ]})

    class ExpandingProvider:
        name = "scripted"
        model = "fixture-model"

        def __init__(self):
            self.calls = []

        def execute(self, worktree, prompt, **_kwargs):
            if "(first)" in prompt:
                self.calls.append("first")
                (Path(worktree) / "first.txt").write_text("accepted\n", encoding="utf-8")
                return ProviderResult("completed", "Done", seconds=0.01)
            assert "(calc)" in prompt
            self.calls.append("calc")
            calc_attempt = self.calls.count("calc")
            if calc_attempt == 3:
                assert "provider changed paths outside the approved task scope" in prompt
                assert "helpers.py" in prompt
            (Path(worktree) / "calc.py").write_text(
                "def add(a, b):\n    return a - b\n" if calc_attempt == 1
                else "from helpers import add\n", encoding="utf-8",
            )
            if calc_attempt >= 2:
                (Path(worktree) / "helpers.py").write_text(
                    "def add(a, b):\n    return a + b\n", encoding="utf-8",
                )
            return ProviderResult("completed", "Done", seconds=0.01)

        def cancel(self):
            pass

    provider = ExpandingProvider()
    orchestrator.planner = Planner()
    orchestrator.provider = provider
    orchestrator.max_task_attempts = 2
    orchestrator.validate_task = lambda path, task: (
        "validation passed" if (Path(path) / "first.txt").read_text(encoding="utf-8") == "accepted\n"
        else "validation failed: first task"
    ) if task.key == "first" else _check_unittest(path, "test_calc")
    orchestrator.validate = lambda path: (
        _check_unittest(path, "test_calc") if (Path(path) / "first.txt").read_text(encoding="utf-8") == "accepted\n"
        else "validation failed: first task"
    )
    events = []
    orchestrator.on_event = lambda kind, payload: events.append((kind, payload))
    run_id, result = _execute(store, orchestrator)
    assert result.status == "awaiting_approval", result.error
    assert "helpers.py" in result.error
    assert provider.calls == ["first", "calc", "calc"]
    revised = store.get_latest_plan_record(run_id)
    assert revised["source"] == "scope_expansion_proposed"
    assert revised["payload"]["tasks"][1]["expected_paths"] == ["calc.py", "helpers.py"]
    tasks = {task.key: task for task in store.list_run_tasks(run_id)}
    assert tasks["first"].status == "completed"
    assert tasks["calc"].status == "waiting_approval"
    accepted = store.get_task_completion(tasks["first"].id)
    assert accepted is not None and accepted.revision == store.get_run(run_id)["final_commit"]
    assert (result.integration_path / "first.txt").read_text(encoding="utf-8") == "accepted\n"
    assert not (result.integration_path / "calc.py").exists()
    assert any(kind == "plan_scope_expansion_proposed" for kind, _ in events)

    class FakeClient:
        def tags(self):
            return [{"name": "molicoder:latest"}]

    window = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(window, "_plan_review_decision", lambda *_args, **_kwargs: "dismiss")
    window._review_build_plan(run_id, orchestrator=orchestrator)
    assert "File scope: calc.py, helpers.py" in window.build_details.toPlainText()
    assert "Reason for fresh approval" in window.build_details.toPlainText()

    changed_completed = {"objective": "Build and repair", "tasks": revised["payload"]["tasks"]}
    changed_completed["tasks"][0]["objective"] = "Rewrite accepted work"
    with pytest.raises(ValueError, match="completed task cannot be changed"):
        orchestrator.revise_plan(run_id, revised["id"], changed_completed)
    with pytest.raises(PermissionError):
        orchestrator.execute_approved(run_id)
    approve(store, run_id)
    resumed = orchestrator.execute_approved(run_id)
    assert resumed.status == "ready_to_apply", resumed.error
    assert provider.calls == ["first", "calc", "calc", "calc"]
    assert store.get_task_completion(tasks["first"].id).revision == accepted.revision
    assert _check_unittest(resumed.integration_path, "test_calc") == "validation passed"
    assert not (root / "helpers.py").exists() and not (root / "calc.py").exists()
    window._build_completed(resumed)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    window._merge_last_build()
    assert store.get_run(run_id)["status"] == "completed"
    assert (root / "first.txt").read_text(encoding="utf-8") == "accepted\n"
    assert _check_unittest(root, "test_calc") == "validation passed"


def test_s070_retry_ceiling_preserves_failure_without_claiming_success(tmp_path):
    root, store, provider, orchestrator, events = _repair_fixture(
        tmp_path,
        ["def add(a, b):\n    return a - b\n",
         "def add(a, b):\n    return a * b\n", CORRECT_ADD],
        ADD_TEST, max_attempts=2,
    )
    run_id, result = _execute(store, orchestrator)
    assert result.status == "needs_attention"
    assert "validation failed" in result.error
    assert len(provider.prompts) == 2
    assert not any(kind == "task_completed" for kind, _ in events)
    assert store.list_run_tasks(run_id)[0].status == "needs_attention"
    assert not (root / "calc.py").exists()
