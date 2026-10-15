from collections import deque

import pytest

from local_model_chat.domain import GenerationMetrics
from local_model_chat.molicoder.gateway import InferenceResponse
from local_model_chat.molicoder.planner.local import LocalPlanner
from local_model_chat.molicoder.planner.schema import PlanValidationError, parse_plan
from local_model_chat.molicoder.recon import ReconPacket


def test_plan_validation_and_dependency_order() -> None:
    plan = parse_plan(
        {
            "objective": "Add feature",
            "tasks": [
                {
                    "key": "tests",
                    "title": "Add tests",
                    "objective": "Add focused tests in tests/test_feature.py.",
                    "risk": "low",
                    "depends_on": [],
                },
                {
                    "key": "implementation",
                    "title": "Implement feature",
                    "objective": "Update src/feature.py until the focused tests pass.",
                    "risk": "medium",
                    "depends_on": ["tests"],
                },
            ],
        }
    )
    assert [task.key for task in plan.ordered_tasks()] == ["tests", "implementation"]


@pytest.mark.parametrize(
    "payload",
    [
        {"objective": "x", "tasks": []},
        {"objective": "x", "tasks": [{"key": "a", "title": "A", "objective": "Do A", "risk": "low", "depends_on": ["missing"]}]},
        {
            "objective": "x",
            "tasks": [
                {"key": "a", "title": "A", "objective": "Do A", "risk": "low", "depends_on": ["b"]},
                {"key": "b", "title": "B", "objective": "Do B", "risk": "low", "depends_on": ["a"]},
            ],
        },
    ],
)
def test_plan_rejects_empty_missing_dependency_and_cycle(payload) -> None:
    with pytest.raises(PlanValidationError):
        parse_plan(payload)


class FakeGateway:
    def __init__(self, contents: list[str]) -> None:
        self.contents = deque(contents)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return InferenceResponse({"content": self.contents.popleft()}, GenerationMetrics())


def test_local_planner_repairs_malformed_plan_once() -> None:
    gateway = FakeGateway(
        [
            '{"objective":"Feature","tasks":[]}',
            (
                '{"objective":"Feature","tasks":['
                '{"key":"task-1","title":"Implement","objective":"Update app.py and run tests.",'
                '"risk":"low","depends_on":[]}]}'
            ),
        ]
    )
    planner = LocalPlanner(gateway=gateway, model="molicoder:latest")
    packet = ReconPacket.empty("Implement feature", "python")

    plan = planner.plan(packet)

    assert plan.tasks[0].key == "task-1"
    assert len(gateway.calls) == 2
    assert "invalid" in gateway.calls[1]["messages"][-1]["content"].lower()


def test_plan_accepts_optional_expected_paths() -> None:
    plan = parse_plan(
        {
            "objective": "Feature",
            "tasks": [
                {
                    "key": "one",
                    "title": "One",
                    "objective": "Edit one file",
                    "risk": "low",
                    "depends_on": [],
                    "expected_paths": ["src/app.py"],
                }
            ],
        }
    )

    assert plan.tasks[0].expected_paths == ("src/app.py",)
    assert plan.tasks[0].acceptance_criteria == ("Configured validation must pass",)
    assert plan.tasks[0].verification_checks == ("project-default",)


def test_plan_records_bounded_task_acceptance_and_named_checks() -> None:
    payload = {
        "objective": "Feature", "tasks": [{
            "key": "one", "title": "One", "objective": "Add parser", "risk": "low",
            "depends_on": [], "expected_paths": ["src/parser.py"],
            "acceptance_criteria": ["Parser accepts valid input", "Parser rejects invalid input"],
            "verification_checks": ["pytest", "lint"],
        }],
    }
    task = parse_plan(payload).tasks[0]
    assert task.acceptance_criteria == ("Parser accepts valid input", "Parser rejects invalid input")
    assert task.verification_checks == ("pytest", "lint")
    payload["tasks"][0]["verification_checks"] = ["python -c 'unsafe'"]
    with pytest.raises(PlanValidationError, match="approved checks"):
        parse_plan(payload)
    payload["tasks"][0]["verification_checks"] = ["pytest"]
    payload["tasks"][0]["acceptance_criteria"] = []
    with pytest.raises(PlanValidationError, match="checkable items"):
        parse_plan(payload)


def test_plan_rejects_more_than_five_tasks() -> None:
    tasks = [
        {
            "key": f"task-{index}", "title": f"Task {index}", "objective": "Do work",
            "risk": "low", "depends_on": [], "expected_paths": [f"file-{index}.txt"],
        }
        for index in range(6)
    ]

    with pytest.raises(PlanValidationError, match="between 1 and 5"):
        parse_plan({"objective": "Too large", "tasks": tasks})
