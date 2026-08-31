import subprocess
import threading

from local_model_chat.molicoder.executor import ExecutionResult
from local_model_chat.molicoder.orchestrator import WorktreeOrchestrator
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.molicoder.repair import RepairOutcome
from local_model_chat.storage import ChatStore


class TwoFilePlanner:
    provider = "test"

    def plan(self, _packet):
        return parse_plan(
            {
                "objective": "two files",
                "tasks": [
                    {"key": "alpha", "title": "Alpha", "objective": "alpha", "risk": "low", "depends_on": [], "expected_paths": ["alpha.txt"]},
                    {"key": "beta", "title": "Beta", "objective": "beta", "risk": "low", "depends_on": [], "expected_paths": ["beta.txt"]},
                ],
            }
        )


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def test_worktree_orchestrator_integrates_without_touching_checkout(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    store = ChatStore(tmp_path / "state.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")

    barrier = threading.Barrier(2, timeout=5)

    def execute(worktree, objective):
        barrier.wait()
        (worktree / f"{objective}.txt").write_text(objective + "\n", encoding="utf-8")
        result = ExecutionResult("completed", "done", 1, 1)
        return RepairOutcome("completed", 1, result, (), "")

    orchestrator = WorktreeOrchestrator(
        store=store,
        project=project,
        planner=TwoFilePlanner(),
        execute_task=execute,
        validate_integration=lambda path: "validation passed" if (path / "alpha.txt").is_file() and (path / "beta.txt").is_file() else "validation failed",
        worktree_base=tmp_path / "trees",
        max_workers=2,
    )

    result = orchestrator.run("make two files")

    assert result.status == "completed"
    assert result.integration_path is not None
    assert (result.integration_path / "alpha.txt").is_file()
    assert (result.integration_path / "beta.txt").is_file()
    assert not (root / "alpha.txt").exists()
    assert _git(root, "status", "--porcelain").stdout == ""
    records = store.list_run_tasks(result.run_id)
    assert all(record.worktree and record.changed_paths for record in records)


def test_worktree_orchestrator_repairs_a_real_merge_conflict(tmp_path) -> None:
    root = tmp_path / "conflict-repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    (root / "shared.txt").write_text("base\n", encoding="utf-8")
    store = ChatStore(tmp_path / "conflict-state.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")

    class ConflictPlanner:
        provider = "test"

        def plan(self, _packet):
            return parse_plan(
                {
                    "objective": "conflict",
                    "tasks": [
                        {"key": "one", "title": "One", "objective": "one", "risk": "low", "depends_on": [], "expected_paths": ["shared.txt"]},
                        {"key": "two", "title": "Two", "objective": "two", "risk": "low", "depends_on": [], "expected_paths": ["shared.txt"]},
                    ],
                }
            )

    def execute(worktree, objective):
        (worktree / "shared.txt").write_text(objective + "\n", encoding="utf-8")
        return RepairOutcome("completed", 1, ExecutionResult("completed", "done", 1, 1), (), "")

    def repair(worktree, _objective):
        (worktree / "shared.txt").write_text("one\ntwo\n", encoding="utf-8")
        return RepairOutcome("completed", 1, ExecutionResult("completed", "resolved", 1, 1), (), "")

    events = []
    orchestrator = WorktreeOrchestrator(
        store=store,
        project=project,
        planner=ConflictPlanner(),
        execute_task=execute,
        repair_conflict=repair,
        validate_integration=lambda path: "validation passed" if (path / "shared.txt").read_text(encoding="utf-8") == "one\ntwo\n" else "validation failed",
        worktree_base=tmp_path / "conflict-trees",
        on_event=lambda kind, payload: events.append(kind),
    )

    result = orchestrator.run("merge both")

    assert result.status == "completed"
    assert "integration_conflict" in events
    assert (result.integration_path / "shared.txt").read_text(encoding="utf-8") == "one\ntwo\n"


def test_worktree_orchestrator_honors_cancellation_before_planning(tmp_path) -> None:
    root = tmp_path / "cancel-repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    store = ChatStore(tmp_path / "cancel.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    planner = TwoFilePlanner()
    orchestrator = WorktreeOrchestrator(
        store=store,
        project=project,
        planner=planner,
        execute_task=lambda *_args: None,
        validate_integration=lambda _path: "validation passed",
        should_cancel=lambda: True,
    )

    result = orchestrator.run("cancel me")

    assert result.status == "cancelled"
    assert result.total_tasks == 0
