import os
import sys
import subprocess

import pytest

from local_model_chat.molicoder.executor import AgentExecutor
from local_model_chat.molicoder.gateway import InferenceGateway
from local_model_chat.molicoder.planner.local import LocalPlanner
from local_model_chat.molicoder.recon import ReconPacket
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.molicoder.runtime import build_orchestrator
from local_model_chat.storage import ChatStore
from local_model_chat.molicoder.tools import ToolDispatcher
from local_model_chat.molicoder.validate import ProjectValidator, ValidationCommand


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MOLICODER_REAL_MODEL") != "1",
        reason="set MOLICODER_REAL_MODEL=1 to exercise the installed Ollama model",
    ),
]


def test_real_model_edits_and_validates_temporary_project(tmp_path) -> None:
    (tmp_path / "sample.py").write_text(
        "def format_duration(seconds: int) -> str:\n    return str(seconds)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_sample.py").write_text(
        "from sample import format_duration\n\n"
        "def test_format_duration():\n"
        "    assert format_duration(65) == '1m 5s'\n",
        encoding="utf-8",
    )
    validator = ProjectValidator(
        tmp_path,
        [ValidationCommand("pytest", (sys.executable, "-m", "pytest", "-q"))],
    )
    executor = AgentExecutor(
        gateway=InferenceGateway(),
        dispatcher=ToolDispatcher(tmp_path, validation=validator.run_and_summarize),
        model="molicoder:latest",
        max_turns=8,
        max_seconds=240,
    )

    result = executor.run(
        "Update only format_duration in sample.py. It must format 65 as '1m 5s': "
        "show minutes and seconds and omit a zero-hours component. Inspect the existing "
        "file and test, make the smallest edit, then run validation."
    )

    assert result.status == "completed", result
    assert validator.run().passed, result


def test_real_model_returns_a_valid_multi_task_plan() -> None:
    planner = LocalPlanner(gateway=InferenceGateway(), model="molicoder:latest")
    packet = ReconPacket.empty(
        "Plan exactly two dependent tasks: first add a duration formatter, then add its unit tests.",
        "python",
    )

    plan = planner.plan(packet)

    assert len(plan.tasks) == 2, plan
    assert plan.ordered_tasks() == plan.tasks, plan


def _create_e2e_project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='e2e'\n", encoding="utf-8")
    (root / "alpha.py").write_text("def value():\n    return 'old-a'\n", encoding="utf-8")
    (root / "beta.py").write_text("def value():\n    return 'old-b'\n", encoding="utf-8")
    (root / "test_alpha.py").write_text("from alpha import value\n\ndef test_value():\n    assert value() == 'old-a'\n", encoding="utf-8")
    (root / "test_beta.py").write_text("from beta import value\n\ndef test_value():\n    assert value() == 'old-b'\n", encoding="utf-8")
    store = ChatStore(tmp_path / "state.sqlite3")
    project = ProjectRegistry(store).add(root)
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "base"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return root, store, project


def test_real_local_multi_task_worktree_run(tmp_path) -> None:
    root, store, project = _create_e2e_project(tmp_path)
    orchestrator = build_orchestrator(store=store, project=project, mode="LOCAL_ONLY")

    result = orchestrator.run(
        "Create exactly two independent tasks. Task alpha must update alpha.py and test_alpha.py "
        "so value() returns 'new-a'. Task beta must update beta.py and test_beta.py so value() "
        "returns 'new-b'. Keep each module and its test together in one task."
    )

    assert result.status == "completed", result
    assert result.total_tasks == 2, result
    assert (result.integration_path / "alpha.py").read_text(encoding="utf-8").find("new-a") >= 0
    assert (result.integration_path / "beta.py").read_text(encoding="utf-8").find("new-b") >= 0
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True)
    assert status.stdout == ""


@pytest.mark.skipif(
    os.environ.get("MOLICODER_REAL_CODEX") != "1",
    reason="set MOLICODER_REAL_CODEX=1 to spend Codex quota on frontier planning and review",
)
def test_real_frontier_plan_local_execution_and_frontier_review(tmp_path) -> None:
    _root, store, project = _create_e2e_project(tmp_path)
    from datetime import datetime, timedelta, timezone

    scope = "opt-in-live-frontier-test"
    store.record_action_authorization(
        project_id=project.id, action="build_run", scope=scope, risk="external",
        decision="approved", actor="explicit_live_test_env",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    orchestrator = build_orchestrator(
        store=store, project=project, mode="FRONTIER_PLAN", authorization_scope=scope,
    )

    result = orchestrator.run(
        "Create exactly two independent tasks. Task alpha must update alpha.py and test_alpha.py "
        "so value() returns 'new-a'. Task beta must update beta.py and test_beta.py so value() "
        "returns 'new-b'. Keep each module and its test together in one task."
    , mode="FRONTIER_PLAN")

    assert result.status == "completed", result
    purposes = [call["purpose"] for call in store.list_external_calls(result.run_id)]
    assert purposes == ["planning", "review"]
