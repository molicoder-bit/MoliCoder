import json

from local_model_chat.molicoder.executor import ExecutionResult
from local_model_chat.molicoder.orchestrator import Phase2Orchestrator
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.registry import ProjectRegistry
from local_model_chat.molicoder.repair import RepairOutcome
from local_model_chat.storage import ChatStore


class FakePlanner:
    provider = "local-test"

    def plan(self, _packet):
        return parse_plan(
            {
                "objective": "Two changes",
                "tasks": [
                    {"key": "one", "title": "First", "objective": "Do first.py", "risk": "low", "depends_on": []},
                    {"key": "two", "title": "Second", "objective": "Do second.py", "risk": "low", "depends_on": ["one"]},
                ],
            }
        )


class FakeRepair:
    def __init__(self):
        self.objectives = []

    def run(self, objective):
        self.objectives.append(objective)
        execution = ExecutionResult("completed", "done", 1, 1)
        return RepairOutcome("completed", 1, execution, (), "")


def test_orchestrator_persists_plan_tasks_events_and_files(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    store = ChatStore(tmp_path / "state.sqlite3")
    project = ProjectRegistry(store).add(root)
    repair = FakeRepair()
    events = []
    orchestrator = Phase2Orchestrator(
        store=store,
        project=project,
        planner=FakePlanner(),
        repair=repair,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )

    result = orchestrator.run("Make two changes", mode="LOCAL_ONLY")

    assert result.status == "completed"
    assert repair.objectives == ["Do first.py", "Do second.py"]
    assert [task.status for task in store.list_run_tasks(result.run_id)] == ["completed", "completed"]
    assert [event.kind for event in store.list_events(result.run_id)][-1] == "run_completed"
    plan_file = root / ".molicoder" / "plans" / f"plan-{result.run_id}.json"
    assert json.loads(plan_file.read_text(encoding="utf-8"))["objective"] == "Two changes"
    assert (root / ".molicoder" / "tasks" / "task-one.json").is_file()
    assert events[0][0] == "run_started"
