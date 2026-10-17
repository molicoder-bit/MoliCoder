"""Observable Build project preflight and plan ownership scenarios."""

from __future__ import annotations

import subprocess

import pytest
from PySide6.QtWidgets import QMessageBox

from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow


class FakeClient:
    def tags(self):
        return [{"name": "molicoder:latest"}]


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def project_root(tmp_path, name, *, repository=True):
    root = tmp_path / name
    root.mkdir()
    if repository:
        (root / ".gitignore").write_text(".molicoder/\n", encoding="utf-8")
        git(root, "init", "-b", "main")
        git(root, "config", "user.email", "fixture@example.test")
        git(root, "config", "user.name", "Fixture")
        git(root, "add", ".")
        git(root, "commit", "-m", "base")
    return root


def window_for(qtbot, store):
    window = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    return window


def test_s011_build_without_selected_project_stays_disabled(qtbot, tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    window = window_for(qtbot, store)
    window.build_request.setPlainText("Create hello.txt")
    window._refresh_build_readiness()
    assert not window.build_run_button.isEnabled()
    assert "Select a project" in window.build_status.text()
    window._run_build()
    assert store.list_projects() == []


def test_s012_valid_project_is_selected_for_build_preflight(qtbot, tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    root = project_root(tmp_path, "project")
    project = store.upsert_project("project", str(root), "python")
    window = window_for(qtbot, store)
    window._load_project_list(select_id=project.id)
    window.build_request.setPlainText("Create hello.txt")
    window._refresh_build_readiness()
    assert window.registry.selected().id == project.id
    assert window.build_run_button.isEnabled()
    assert str(root) in window.build_project_meta.text()


def test_s013_missing_project_folder_blocks_build(qtbot, tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    project = store.upsert_project("missing", str(tmp_path / "missing"), "python")
    window = window_for(qtbot, store)
    window._load_project_list(select_id=project.id)
    window._refresh_build_readiness()
    assert not window.build_run_button.isEnabled()
    assert "folder is missing" in window.build_status.text()


def test_s015_non_git_project_is_explicitly_blocked(qtbot, tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    root = project_root(tmp_path, "plain", repository=False)
    project = store.upsert_project("plain", str(root), "python")
    window = window_for(qtbot, store)
    window._load_project_list(select_id=project.id)
    window.build_request.setPlainText("Create hello.txt")
    window._refresh_build_readiness()
    assert not window.build_run_button.isEnabled()
    assert "requires a Git repository" in window.build_status.text()


def test_s014_read_only_project_blocks_before_planning(qtbot, tmp_path, monkeypatch):
    store = ChatStore(tmp_path / "state.sqlite3")
    root = project_root(tmp_path, "readonly")
    project = store.upsert_project("readonly", str(root), "python")
    window = window_for(qtbot, store)
    window._load_project_list(select_id=project.id)
    window.build_request.setPlainText("Create hello.txt")

    def denied(*_args, **_kwargs):
        raise PermissionError("read-only fixture")

    monkeypatch.setattr("tempfile.TemporaryFile", denied)
    window._run_build()
    assert "not writable" in window.build_status.text()
    assert store.list_project_runs(project.id) == []


@pytest.mark.parametrize("scenario,requested_text,fragment", [
    ("S016", "Fix the thing.", "Clarify which file or behavior"),
    ("S017", "Delete this file and keep it unchanged.", "same file cannot be deleted"),
    ("S018", "Deploy this app to production.", "not a supported Build action"),
])
def test_s016_s017_s018_invalid_or_unsupported_request_waits_for_clarification(
    qtbot, tmp_path, scenario, requested_text, fragment,
):
    store = ChatStore(tmp_path / "state.sqlite3")
    root = project_root(tmp_path, "project")
    project = store.upsert_project("project", str(root), "python")
    window = window_for(qtbot, store)
    window._load_project_list(select_id=project.id)
    window.build_request.setPlainText(requested_text)
    window._run_build()
    assert fragment in window.build_status.text()
    assert store.list_project_runs(project.id) == []


def test_s019_pending_plan_cannot_be_approved_from_another_project(qtbot, tmp_path, monkeypatch):
    store = ChatStore(tmp_path / "state.sqlite3")
    root_a = project_root(tmp_path, "first")
    root_b = project_root(tmp_path, "second")
    first = store.upsert_project("first", str(root_a), "python")
    second = store.upsert_project("second", str(root_b), "python")
    run_id = store.create_run(first.id, "Create hello.txt", "HYBRID", provider="codex")
    store.save_plan(run_id, "generated", "fixture", {
        "objective": "Create hello.txt", "tasks": [{
            "key": "create", "title": "Create", "objective": "Create hello.txt", "risk": "low",
            "depends_on": [], "expected_paths": ["hello.txt"],
            "acceptance_criteria": ["Configured validation must pass"],
            "verification_checks": ["project-default"],
        }],
    })
    store.update_run_state(run_id, "awaiting_approval", base_commit=git(root_a, "rev-parse", "HEAD"))
    window = window_for(qtbot, store)
    window._load_project_list(select_id=second.id)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: pytest.fail("Wrong-project approval was shown"))
    window._review_build_plan(run_id)
    assert "Select the plan's project" in window.build_status.text()
    assert store.get_run(run_id)["project_id"] == first.id


def test_s020_chat_status_answer_reads_persisted_state_without_starting_build(qtbot, tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    root = project_root(tmp_path, "project")
    project = store.upsert_project("project", str(root), "python")
    run_id = store.create_run(project.id, "Implement search", "HYBRID")
    task_id = store.create_run_task(run_id, "index", "Add index", "Index files", "low")
    store.update_run_task(task_id, "running", 1)
    window = window_for(qtbot, store)
    window._load_project_list(select_id=project.id)
    recorded_status = store.get_run(run_id)["status"]
    window.composer.setPlainText("What are we working on?")
    window._send_message()
    qtbot.waitUntil(lambda: window.chat_worker is None, timeout=10000)
    messages = store.list_messages(window.current_conversation_id)
    assert "Add index" in messages[-1].content
    assert f"run #{run_id}" in messages[-1].content
    assert recorded_status in messages[-1].content
    assert store.get_run(run_id)["status"] == recorded_status
    assert len(store.list_project_runs(project.id)) == 1
    assert window.orchestration_worker is None
