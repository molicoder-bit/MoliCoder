"""Approved execution must use the latest validated task graph revision."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult, ProviderStatus
from test_build_plan_approval import approve, fixture


def task(key: str, path: str, depends=()):
    return {"key": key, "title": key, "objective": f"Write {path}", "risk": "low",
            "depends_on": list(depends), "expected_paths": [path],
            "acceptance_criteria": ["Configured validation must pass"],
            "verification_checks": ["project-default"]}


class FileExecutor:
    name = "scripted"
    model = "fixture-model"

    def __init__(self):
        self.calls = []

    def execute(self, worktree, prompt, *, timeout_seconds=1200):
        key = next(key for key in ("write", "first", "second") if f"({key})" in prompt)
        self.calls.append(key)
        filename = {"write": "hello.txt", "first": "first.txt", "second": "second.txt"}[key]
        (Path(worktree) / filename).write_text(key + "\n", encoding="utf-8")
        return ProviderResult("completed", "done", seconds=0.01)

    def cancel(self):
        pass


def configure_two_file_validation(orchestrator, filenames):
    orchestrator.validate_task = lambda path, current: (
        "validation passed" if all((Path(path) / name).is_file() for name in current.expected_paths)
        else "validation failed: task output missing"
    )
    orchestrator.validate = lambda path: (
        "validation passed" if all((Path(path) / name).is_file() for name in filenames)
        else "validation failed: integrated output missing"
    )


def test_s034_add_task_through_review_requires_new_approval(tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from local_model_chat.ui.main_window import MainWindow

    root, store, project, original_executor, orchestrator = fixture(tmp_path, "hello.txt", "write\n")
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    original = store.get_latest_plan_record(prepared.run_id)
    approve(store, prepared.run_id)
    edited = {"objective": "Write two files", "tasks": [
        original["payload"]["tasks"][0], task("second", "second.txt", ("write",)),
    ]}

    class FakeClient:
        def tags(self):
            return [{"name": "molicoder:latest"}]

    window = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    decisions = iter(("edit", "dismiss"))
    monkeypatch.setattr(window, "_plan_review_decision", lambda *_args, **_kwargs: next(decisions))
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *_args: (json.dumps(edited), True))
    window._review_build_plan(prepared.run_id, orchestrator=orchestrator)

    revised = store.get_latest_plan_record(prepared.run_id)
    assert revised["id"] > original["id"]
    assert [item["key"] for item in revised["payload"]["tasks"]] == ["write", "second"]
    assert set(revised["payload"]["task_assignments"]) == {"write", "second"}
    assert original_executor.calls == 0
    with pytest.raises(PermissionError):
        orchestrator.execute_approved(prepared.run_id)

    executor = FileExecutor()
    orchestrator.provider = executor
    configure_two_file_validation(orchestrator, ("hello.txt", "second.txt"))
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert executor.calls == ["write", "second"]
    assert (result.integration_path / "second.txt").read_text(encoding="utf-8") == "second\n"
    window.last_run_id = prepared.run_id
    window.last_integration_branch = result.integration_branch
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: pytest.fail("Apply was unexpectedly blocked"))
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: pytest.fail("Apply unexpectedly failed"))
    window._merge_last_build()
    assert (root / "second.txt").read_text(encoding="utf-8") == "second\n"
    assert store.get_run(prepared.run_id)["status"] == "completed"


def test_s035_removing_parent_rejects_dangling_edge_then_saves_valid_revision(tmp_path):
    _root, store, _project, executor, orchestrator = fixture(tmp_path, "hello.txt", "hello\n")

    class TwoTaskPlanner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Two tasks", "tasks": [
                task("write", "hello.txt"), task("second", "second.txt", ("write",)),
            ]})

    orchestrator.planner = TwoTaskPlanner()
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    original = store.get_latest_plan_record(prepared.run_id)
    with pytest.raises(ValueError, match="missing dependency"):
        orchestrator.revise_plan(prepared.run_id, original["id"], {
            "objective": "Invalid removal", "tasks": [task("second", "second.txt", ("write",))],
        })
    assert store.get_latest_plan_record(prepared.run_id)["id"] == original["id"]
    assert executor.calls == 0

    revision = orchestrator.revise_plan(prepared.run_id, original["id"], {
        "objective": "One task", "tasks": [task("write", "hello.txt")],
    })
    assert revision > original["id"]
    assert list(store.get_latest_plan_record(prepared.run_id)["payload"]["task_assignments"]) == ["write"]
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert executor.calls == 1
    assert not (result.integration_path / "second.txt").exists()


def test_s036_edited_dependency_controls_execution_order(tmp_path):
    _root, store, _project, _original_executor, orchestrator = fixture(tmp_path)

    class IndependentPlanner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Independent tasks", "tasks": [
                task("second", "second.txt"), task("first", "first.txt"),
            ]})

    orchestrator.planner = IndependentPlanner()
    prepared = orchestrator.run("Write two files", mode="FRONTIER_ONLY")
    original = store.get_latest_plan_record(prepared.run_id)
    approve(store, prepared.run_id)
    orchestrator.revise_plan(prepared.run_id, original["id"], {
        "objective": "Ordered tasks", "tasks": [
            task("second", "second.txt", ("first",)), task("first", "first.txt"),
        ],
    })
    with pytest.raises(PermissionError):
        orchestrator.execute_approved(prepared.run_id)
    executor = FileExecutor()
    orchestrator.provider = executor
    configure_two_file_validation(orchestrator, ("first.txt", "second.txt"))
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert executor.calls == ["first", "second"]


def test_s037_compatible_task_model_override_is_approved_recorded_and_executed(tmp_path):
    root, store, _project, primary, orchestrator = fixture(tmp_path, "hello.txt", "alternate\n")

    class Alternate:
        name = "alternate"
        model = "alternate-model"

        def __init__(self):
            self.calls = 0

        def probe(self):
            return ProviderStatus(self.name, True, True, model=self.model, auth_verified=True)

        def execute(self, worktree, _prompt, *, timeout_seconds=1200):
            self.calls += 1
            (Path(worktree) / "hello.txt").write_text("alternate\n", encoding="utf-8")
            return ProviderResult("completed", "done", model=self.model)

        def cancel(self):
            pass

    alternate = Alternate()
    orchestrator.alternate_provider = alternate
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    first = store.get_latest_plan_record(prepared.run_id)
    with pytest.raises(ValueError, match="not a configured Build provider model"):
        orchestrator.revise_plan(prepared.run_id, first["id"], {
            "objective": first["payload"]["objective"],
            "tasks": first["payload"]["tasks"],
            "task_model_overrides": {"write": "unconfigured-model"},
        })
    assert store.get_latest_plan_record(prepared.run_id)["id"] == first["id"]
    approve(store, prepared.run_id)
    revision = orchestrator.revise_plan(prepared.run_id, first["id"], {
        "objective": first["payload"]["objective"],
        "tasks": first["payload"]["tasks"],
        "task_model_overrides": {"write": "alternate-model"},
    })
    saved = store.get_latest_plan_record(prepared.run_id)
    assert saved["id"] == revision
    assert saved["payload"]["task_assignments"]["write"]["model"] == "alternate-model"
    assert saved["payload"]["task_assignments"]["write"]["provider"] == "alternate"
    with pytest.raises(PermissionError):
        orchestrator.execute_approved(prepared.run_id)
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert alternate.calls == 1 and primary.calls == 0
    assert (result.integration_path / "hello.txt").read_text(encoding="utf-8") == "alternate\n"
    assert not (root / "hello.txt").exists()


def test_plan_editor_cannot_overwrite_a_newer_revision(tmp_path):
    _root, store, _project, _executor, orchestrator = fixture(tmp_path)
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    original = store.get_latest_plan_record(prepared.run_id)
    draft = {"objective": "First edit", "tasks": [task("write", "hello.txt")]}
    newer = orchestrator.revise_plan(prepared.run_id, original["id"], draft)
    with pytest.raises(ValueError, match="revision changed"):
        orchestrator.revise_plan(prepared.run_id, original["id"], {
            "objective": "Stale edit", "tasks": [task("write", "hello.txt")],
        })
    assert store.get_latest_plan_record(prepared.run_id)["id"] == newer


def test_s038_unsupported_runner_override_is_explained_without_execution(tmp_path, qtbot, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from local_model_chat.ui.main_window import MainWindow

    _root, store, project, executor, orchestrator = fixture(tmp_path)
    prepared = orchestrator.run("Write hello.txt", mode="FRONTIER_ONLY")
    original = store.get_latest_plan_record(prepared.run_id)

    class FakeClient:
        def tags(self):
            return [{"name": "molicoder:latest"}]

    window = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(window, "_plan_review_decision", lambda *_args, **_kwargs: "edit")
    edited = {"objective": "Override route", "tasks": original["payload"]["tasks"],
              "task_assignments": {"write": {"runner": "kandev"}}}
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *_args: (json.dumps(edited), True))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(str(_args[-1])))
    window._review_build_plan(prepared.run_id, orchestrator=orchestrator)
    assert any("cannot override runner or model" in message for message in warnings)
    assert store.get_latest_plan_record(prepared.run_id)["id"] == original["id"]
    assert executor.calls == 0
