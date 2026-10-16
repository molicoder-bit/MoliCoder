"""Deterministic file scenarios through the real sequential Build workflow."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
from local_model_chat.molicoder.artifact_checks import empty_directory_targets, verify_artifact_checks
from local_model_chat.molicoder.plan_approval import approval_scope
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.storage import ChatStore


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def run_file_scenario(tmp_path, *, before, task_specs, execute, verify):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / ".gitignore").write_text(".molicoder/\n", encoding="utf-8")
    for name, content in before.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "config", "user.name", "Fixture")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    store = ChatStore(tmp_path / "state.sqlite3")
    project = ProjectRegistry(store).add(root)

    class Planner:
        provider = "fixture"

        def plan(self, _packet):
            return parse_plan({"objective": "File scenario", "tasks": task_specs})

    class Executor:
        name = "fixture"
        model = "scripted"
        calls = 0

        def execute(self, worktree, prompt, *, timeout_seconds=1200):
            self.calls += 1
            execute(Path(worktree), prompt)
            return ProviderResult("completed", "done", seconds=0.01)

        def cancel(self):
            pass

    executor = Executor()
    validator = lambda path: "validation passed" if verify(Path(path)) else "validation failed: wrong artifact"
    def validate_task(path, current_task):
        if len(task_specs) == 1:
            return validator(path)
        return ("validation passed" if all((Path(path) / name).is_file()
                                       for name in current_task.expected_paths)
                else "validation failed: task artifact missing")
    orchestrator = SequentialRunOrchestrator(
        store=store, project=project, planner=Planner(), provider=executor,
        validate=validator, validate_task=validate_task,
        worktree_base=tmp_path / "worktrees", auto_apply=False,
        require_plan_approval=True,
    )
    prepared = orchestrator.run("Perform file scenario", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval" and executor.calls == 0
    plan = store.get_latest_plan_record(prepared.run_id)
    run = store.get_run(prepared.run_id)
    store.record_action_authorization(
        project_id=project.id, action="build_plan_execute",
        scope=approval_scope(run, plan["id"], plan["payload"]), risk="mutation",
        decision="approved", actor="scenario_harness",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert verify(result.integration_path)
    assert executor.calls == len(task_specs)
    assert git(root, "status", "--porcelain") == ""
    assert all(task.status == "completed" for task in store.list_run_tasks(prepared.run_id))
    return result, store


def task(key, paths, depends=()):
    return {"key": key, "title": key, "objective": key, "risk": "low",
            "depends_on": list(depends), "expected_paths": list(paths),
            "acceptance_criteria": ["Configured validation must pass"],
            "verification_checks": ["project-default"]}


@pytest.mark.parametrize("scenario,before,paths,action,expected", [
    ("S003", {"message.txt": "Hello World\n"}, ("message.txt",), "replace", {"message.txt": "Hello MoliCode\n"}),
    ("S004", {"message.txt": "one\n"}, ("message.txt",), "append", {"message.txt": "one\ntwo\n"}),
    ("S005", {"old.txt": "kept\n"}, ("old.txt", "new.txt"), "rename", {"new.txt": "kept\n"}),
    ("S006", {"temporary.txt": "remove\n", "keep.txt": "keep\n"}, ("temporary.txt",), "delete", {"keep.txt": "keep\n"}),
    ("S009", {"settings.json": '{"target": 1, "other": 7}\n'}, ("settings.json",), "config", {"settings.json": '{"target": 2, "other": 7}\n'}),
])
def test_s003_s004_s005_s006_s009_exact_file_operations(tmp_path, scenario, before, paths, action, expected):
    def execute(worktree, _prompt):
        if action == "replace":
            (worktree / "message.txt").write_text("Hello MoliCode\n", encoding="utf-8")
        elif action == "append":
            with (worktree / "message.txt").open("a", encoding="utf-8") as stream:
                stream.write("two\n")
        elif action == "rename":
            (worktree / "old.txt").rename(worktree / "new.txt")
        elif action == "delete":
            (worktree / "temporary.txt").unlink()
        elif action == "config":
            path = worktree / "settings.json"
            settings = json.loads(path.read_text(encoding="utf-8"))
            settings["target"] = 2
            path.write_text(json.dumps(settings) + "\n", encoding="utf-8")

    def verify(worktree):
        names = {path.name for path in worktree.iterdir() if path.is_file()
                 and path.name not in {"pyproject.toml", ".git", ".gitignore"}}
        return (names == set(expected)
                and all((worktree / name).read_text(encoding="utf-8") == content
                        for name, content in expected.items()))

    run_file_scenario(tmp_path, before=before, task_specs=[task("edit", paths)],
                      execute=execute, verify=verify)


def test_s008_independent_files_have_no_dependency_edge(tmp_path):
    def execute(worktree, prompt):
        if "(first)" in prompt:
            (worktree / "first.txt").write_text("first\n", encoding="utf-8")
        else:
            (worktree / "second.txt").write_text("second\n", encoding="utf-8")

    def verify(worktree):
        return ((worktree / "first.txt").read_text(encoding="utf-8") == "first\n"
                and (worktree / "second.txt").read_text(encoding="utf-8") == "second\n")

    result, store = run_file_scenario(
        tmp_path, before={}, task_specs=[task("first", ("first.txt",)), task("second", ("second.txt",))],
        execute=execute, verify=verify,
    )
    plan = store.get_run_plan(result.run_id)
    assert all(not item["depends_on"] for item in plan["tasks"])


def test_s007_empty_directory_survives_review_and_apply(tmp_path, qtbot, monkeypatch):
    spec = task("create-directory", ("reports",))
    spec["artifact_checks"] = [{"kind": "directory_empty", "path": "reports"}]

    def execute(worktree, _prompt):
        (worktree / "reports").mkdir()

    result, store = run_file_scenario(
        tmp_path, before={}, task_specs=[spec], execute=execute,
        verify=lambda worktree: (worktree / "reports").is_dir()
        and not any((worktree / "reports").iterdir()),
    )
    root = tmp_path / "project"
    plan = parse_plan(store.get_run_plan(result.run_id))
    targets = empty_directory_targets(result.integration_path, root, plan)
    assert targets == (root / "reports",)
    assert not (root / "reports").exists()
    assert result.final_commit == result.base_commit  # Git cannot represent an empty directory.
    from PySide6.QtWidgets import QMessageBox
    from local_model_chat.ui.main_window import MainWindow

    class FakeClient:
        def tags(self):
            return [{"name": "molicoder:latest"}]

    window = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=store.get_run(result.run_id)["project_id"])
    window.last_run_id = result.run_id
    window.last_integration_branch = result.integration_branch
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: pytest.fail("Apply was unexpectedly blocked"))
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: pytest.fail("Apply unexpectedly failed"))
    window._merge_last_build()
    assert verify_artifact_checks(root, plan.tasks[0].artifact_checks) == ()
    assert list((root / "reports").iterdir()) == []
    assert store.get_run(result.run_id)["status"] == "completed"


def test_s010_exact_already_satisfied_request_has_verified_no_change(tmp_path):
    spec = task("already-done", ("message.txt",))
    spec["artifact_checks"] = [{"kind": "file_equals", "path": "message.txt", "content": "Hello MoliCode\n"}]
    result, store = run_file_scenario(
        tmp_path, before={"message.txt": "Hello MoliCode\n"},
        task_specs=[spec], execute=lambda _worktree, _prompt: None,
        verify=lambda worktree: (worktree / "message.txt").read_text(encoding="utf-8") == "Hello MoliCode\n",
    )
    assert result.final_commit == result.base_commit
    task_record = store.list_run_tasks(result.run_id)[0]
    assert task_record.changed_paths == ()
    completion = store.get_task_completion(task_record.id)
    assert completion is not None and "message.txt" in completion.output_artifacts
    assert git(tmp_path / "project", "status", "--porcelain") == ""


def test_no_change_rejected_when_explicit_artifact_check_fails(tmp_path):
    spec = task("incorrect", ("message.txt",))
    spec["artifact_checks"] = [{"kind": "file_equals", "path": "message.txt", "content": "desired\n"}]
    with pytest.raises(AssertionError, match="needs_attention"):
        run_file_scenario(
            tmp_path, before={"message.txt": "actual\n"}, task_specs=[spec],
            execute=lambda _worktree, _prompt: None,
            verify=lambda worktree: (worktree / "message.txt").is_file(),
        )
