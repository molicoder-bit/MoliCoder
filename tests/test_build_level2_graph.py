"""Invalid task graphs must fail before approval or scheduling."""

from __future__ import annotations

import pytest

from local_model_chat.molicoder.planner.schema import parse_plan
from test_build_plan_approval import fixture


def spec(key, dependencies):
    return {"key": key, "title": key, "objective": f"Do {key}", "risk": "low",
            "depends_on": dependencies, "expected_paths": [f"{key}.txt"],
            "acceptance_criteria": ["Configured validation must pass"],
            "verification_checks": ["project-default"]}


@pytest.mark.parametrize("scenario,tasks,reason", [
    ("S028", [spec("a", ["c"]), spec("b", ["a"]), spec("c", ["b"])], "cycle"),
    ("S029", [spec("a", ["missing"])], "missing dependency"),
])
def test_s028_s029_invalid_graph_never_reaches_execution(tmp_path, scenario, tasks, reason):
    root, store, _project, executor, orchestrator = fixture(tmp_path)

    class InvalidPlanner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Invalid graph", "tasks": tasks})

    orchestrator.planner = InvalidPlanner()
    result = orchestrator.run("Perform invalid task graph", mode="FRONTIER_ONLY")
    assert result.status == "needs_attention"
    assert reason in result.error
    assert executor.calls == 0
    assert store.get_latest_plan_record(result.run_id) is None
    assert store.list_run_tasks(result.run_id) == []
    assert not (tmp_path / "worktrees" / f"run-{result.run_id}" / "workspace").exists()
    assert root.is_dir()
