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
        task_name = "alpha" if "Assigned task: Alpha" in objective else "beta"
        (worktree / f"{task_name}.txt").write_text(task_name + "\n", encoding="utf-8")
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


def test_failed_task_event_contains_executor_diagnostics(tmp_path) -> None:
    root = tmp_path / "failed-repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    store = ChatStore(tmp_path / "failed.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")

    def execute(_worktree, _objective):
        execution = ExecutionResult(
            "budget_exhausted",
            "",
            4,
            0,
            "turn budget exceeded",
            ("turn 4: no tool call; model said: I should inspect first",),
            7053,
            240,
            3.65,
        )
        return RepairOutcome("failed", 4, execution, ("fingerprint",), "attempt_budget")

    events = []
    orchestrator = WorktreeOrchestrator(
        store=store,
        project=project,
        planner=TwoFilePlanner(),
        execute_task=execute,
        validate_integration=lambda _path: "validation passed",
        worktree_base=tmp_path / "failed-trees",
        max_workers=1,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )

    result = orchestrator.run("fail visibly")

    assert result.status == "failed"
    failure_payload = next(payload for kind, payload in events if kind == "task_failed")
    assert failure_payload["reason"] == "attempt_budget"
    assert failure_payload["executor_error"] == "turn budget exceeded"
    assert failure_payload["turns"] == 4
    assert failure_payload["tool_calls"] == 0
    assert "inspect first" in failure_payload["trace"][-1]


def test_worker_receives_original_request_plan_context_and_expected_paths(tmp_path) -> None:
    root = tmp_path / "context-repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    store = ChatStore(tmp_path / "context.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")

    class OneTaskPlanner:
        provider = "test"

        def plan(self, _packet):
            return parse_plan(
                {
                    "objective": "Design a calculator with a fancy UI",
                    "tasks": [
                        {
                            "key": "structure",
                            "title": "Create project structure",
                            "objective": "Set up the basic project structure.",
                            "risk": "low",
                            "depends_on": [],
                            "expected_paths": ["src/calculator/ui.py", "tests/test_calculator.py"],
                        }
                    ],
                }
            )

    received = []

    def execute(worktree, objective):
        received.append(objective)
        (worktree / "created.txt").write_text("done\n", encoding="utf-8")
        return RepairOutcome("completed", 1, ExecutionResult("completed", "done", 1, 1), (), "")

    orchestrator = WorktreeOrchestrator(
        store=store,
        project=project,
        planner=OneTaskPlanner(),
        execute_task=execute,
        validate_integration=lambda _path: "validation passed",
        worktree_base=tmp_path / "context-trees",
    )

    result = orchestrator.run("Ok design me a calculator with a fancy UI")

    assert result.status == "completed"
    assert len(received) == 1
    worker_prompt = received[0]
    assert "Ok design me a calculator with a fancy UI" in worker_prompt
    assert "Design a calculator with a fancy UI" in worker_prompt
    assert "Create project structure" in worker_prompt
    assert "Set up the basic project structure." in worker_prompt
    assert "src/calculator/ui.py" in worker_prompt
    assert "tests/test_calculator.py" in worker_prompt
    assert "Verified repository paths" in worker_prompt
    assert "pyproject.toml" in worker_prompt


def test_parallel_worker_statuses_are_finalized_when_a_sibling_fails(tmp_path) -> None:
    root = tmp_path / "parallel-failure-repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    store = ChatStore(tmp_path / "parallel-failure.sqlite3")
    project = ProjectRegistry(store).add(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    barrier = threading.Barrier(2, timeout=5)

    def execute(worktree, objective):
        barrier.wait()
        if "Assigned task: Alpha" in objective:
            execution = ExecutionResult("failed", "", 1, 0, "model refused")
            return RepairOutcome("failed", 1, execution, ("failed",), "attempt_budget")
        (worktree / "beta.txt").write_text("beta\n", encoding="utf-8")
        execution = ExecutionResult("completed", "done", 1, 1)
        return RepairOutcome("completed", 1, execution, (), "")

    orchestrator = WorktreeOrchestrator(
        store=store,
        project=project,
        planner=TwoFilePlanner(),
        execute_task=execute,
        validate_integration=lambda _path: "validation passed",
        worktree_base=tmp_path / "parallel-failure-trees",
        max_workers=2,
    )

    result = orchestrator.run("run parallel tasks")

    assert result.status == "failed"
    statuses = {record.key: record.status for record in store.list_run_tasks(result.run_id)}
    assert "running" not in statuses.values()
    assert statuses == {"alpha": "failed", "beta": "completed"}
