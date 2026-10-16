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


def test_s030_fragmented_trivial_plan_is_flagged_for_review_without_losing_checks(tmp_path, qtbot, monkeypatch):
    from local_model_chat.ui.main_window import MainWindow

    root, store, project, executor, orchestrator = fixture(tmp_path)
    tasks = [
        {**spec(f"fragment-{index}", [f"fragment-{index - 1}"] if index > 1 else []),
         "expected_paths": ["hello.txt"]}
        for index in range(1, 6)
    ]

    class FragmentedPlanner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Write one trivial file", "tasks": tasks})

    orchestrator.planner = FragmentedPlanner()
    prepared = orchestrator.run("Write one trivial file", mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval"
    saved = store.get_latest_plan_record(prepared.run_id)["payload"]
    assert len(saved["tasks"]) == 5
    assert all(task["verification_checks"] == ["project-default"] for task in saved["tasks"])
    assert any("excessive fragmentation" in warning for warning in saved["review_warnings"])

    class FakeClient:
        def tags(self):
            return [{"name": "molicoder:latest"}]

    window = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(window, "_plan_review_decision", lambda *_args, **_kwargs: "dismiss")
    window._review_build_plan(prepared.run_id, orchestrator=orchestrator)
    assert "excessive fragmentation" in window.build_details.toPlainText()
    assert executor.calls == 0
    assert not (root / "hello.txt").exists()
