import subprocess
from collections import deque

from local_model_chat.domain import GenerationMetrics
from local_model_chat.molicoder.executor import AgentExecutor
from local_model_chat.molicoder.gateway import InferenceResponse
from local_model_chat.molicoder.orchestrator import WorktreeOrchestrator
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.projectstate import ensure_project_state
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.molicoder.repair import RepairLoop
from local_model_chat.molicoder.runtime import build_validator
from local_model_chat.molicoder.tools import ToolDispatcher
from local_model_chat.storage import ChatStore


class CalculatorPlanner:
    provider = "deterministic-test"

    def plan(self, _packet):
        return parse_plan(
            {
                "objective": "build a tested calculator",
                "tasks": [
                    {
                        "key": "calculator",
                        "title": "Create calculator and tests",
                        "objective": "Create calculator.py and test_calculator.py, then validate them.",
                        "risk": "low",
                        "depends_on": [],
                        "expected_paths": ["calculator.py", "test_calculator.py"],
                    }
                ],
            }
        )


class ScriptedModel:
    def __init__(self) -> None:
        self.responses = deque(
            [
                {
                    "content": '{"name":"write_file","arguments":{"path":"calculator.py",'
                    '"content":"def add(left, right):\\n    return left + right\\n"}}'
                },
                {
                    "content": '{"name":"write_file","arguments":{"path":"test_calculator.py",'
                    '"content":"from calculator import add\\n\\ndef test_add():\\n    assert add(2, 3) == 5\\n"}}'
                },
                {"content": '{"name":"run_validation","arguments":{}}'},
                {"content": "Created and validated the calculator."},
            ]
        )

    def chat(self, **_kwargs) -> InferenceResponse:
        return InferenceResponse(
            self.responses.popleft(),
            GenerationMetrics(prompt_tokens=20, output_tokens=10),
        )


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )


def test_full_local_build_workflow_creates_validated_integration_branch(tmp_path) -> None:
    root = tmp_path / "calculator-project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname='calculator-project'\nversion='0.1.0'\n", encoding="utf-8"
    )
    ensure_project_state(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    store = ChatStore(tmp_path / "workflow.sqlite3")
    project = ProjectRegistry(store).add(root)

    def execute(worktree, objective):
        validator = build_validator(worktree, environment_root=root)
        executor = AgentExecutor(
            gateway=ScriptedModel(),
            dispatcher=ToolDispatcher(worktree, validation=validator.run_and_summarize),
            model="scripted-test-model",
            max_turns=6,
        )
        return RepairLoop(
            execute=executor.run,
            diagnose=lambda failure: failure,
            max_attempts=2,
        ).run(objective)

    events = []
    orchestrator = WorktreeOrchestrator(
        store=store,
        project=project,
        planner=CalculatorPlanner(),
        execute_task=execute,
        validate_integration=lambda worktree: build_validator(
            worktree, environment_root=root
        ).run_and_summarize(),
        worker_model="scripted-test-model",
        max_workers=1,
        worktree_base=tmp_path / "worktrees",
        on_event=lambda kind, payload: events.append((kind, payload)),
    )

    result = orchestrator.run("Build a small tested calculator", mode="LOCAL_ONLY")

    assert result.status == "completed"
    assert result.completed_tasks == result.total_tasks == 1
    assert result.integration_path is not None
    assert (result.integration_path / "calculator.py").is_file()
    assert (result.integration_path / "test_calculator.py").is_file()
    assert build_validator(
        result.integration_path, environment_root=root
    ).run_and_summarize().startswith("validation passed")
    assert not (root / "calculator.py").exists()
    assert _git(root, "status", "--porcelain").stdout == ""
    assert any(kind == "integration_completed" for kind, _payload in events)
    log_path = root / ".molicoder" / "logs" / f"run-{result.run_id}.jsonl"
    assert log_path.is_file()
    log_text = log_path.read_text(encoding="utf-8")
    assert '"kind": "run_started"' in log_text
    assert '"kind": "task_completed"' in log_text
    assert '"kind": "run_completed"' in log_text
