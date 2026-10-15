import subprocess

import pytest

from local_model_chat.controller import MainController
from local_model_chat.storage import ChatStore


def test_current_task_answer_is_source_backed_and_read_only(tmp_path) -> None:
    store = ChatStore(tmp_path / "status.sqlite3")
    project = store.upsert_project("Example", str(tmp_path / "example"), "python")
    run_id = store.create_run(project.id, "Implement search", "HYBRID")
    task_id = store.create_run_task(run_id, "T1", "Add index", "Index files", "low")
    store.update_run_task(task_id, "running", 1)
    controller = MainController(store)

    result = controller.handle("What task are we on?", project.id)

    assert result is not None
    assert result.kind == "task_status"
    assert "Example" in result.content
    assert f"run #{run_id}" in result.content
    assert "T1" in result.content and "Add index" in result.content
    assert "Last updated:" in result.content
    assert result.evidence == f"SQLite project {project.id}, run {run_id}"
    assert store.get_run(run_id)["status"] == "running"


def test_current_task_handles_missing_project_and_no_run(tmp_path) -> None:
    store = ChatStore(tmp_path / "status.sqlite3")
    controller = MainController(store)
    project = store.upsert_project("Empty", str(tmp_path / "empty"), "python")

    assert "Select a project" in controller.handle("Current task", None).content
    assert "no longer available" in controller.handle("Current task", 999).content
    assert "no recorded Build run" in controller.handle("Current task", project.id).content


def test_only_explicit_status_questions_use_controller(tmp_path) -> None:
    controller = MainController(ChatStore(tmp_path / "status.sqlite3"))

    for message in ("Hi", "Can you implement the current task?", "What task should we do next?"):
        assert controller.handle(message, None) is None


@pytest.mark.parametrize("task_state", ["completed", "running", "blocked"])
def test_task_state_is_reported_without_guessing(tmp_path, task_state: str) -> None:
    store = ChatStore(tmp_path / f"{task_state}.sqlite3")
    project = store.upsert_project("Example", str(tmp_path / task_state), "python")
    run_id = store.create_run(project.id, "Work", "LOCAL_ONLY")
    task_id = store.create_run_task(run_id, "T1", "One task", "Work", "low")
    store.update_run_task(task_id, task_state, 1)

    answer = MainController(store).handle("Task status", project.id)

    assert answer is not None
    assert f"**{task_state}**" in answer.content


def test_git_version_and_changed_files_are_read_without_inference(tmp_path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "file.txt").write_text("original", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "base"], check=True, capture_output=True)
    (root / "file.txt").write_text("changed", encoding="utf-8")
    store = ChatStore(tmp_path / "git.sqlite3")
    project = store.upsert_project("Repo", str(root), "python")
    controller = MainController(store)

    version = controller.handle("What commit are we on?", project.id)
    changes = controller.handle("Show changed files", project.id)

    assert version is not None and "main" in version.content and "commit" in version.content
    assert changes is not None and "file.txt" in changes.content
    assert "git status --short" in changes.evidence
