from collections import deque

from local_model_chat.molicoder.executor import ExecutionResult
from local_model_chat.molicoder.repair import RepairLoop


def failed(message: str) -> ExecutionResult:
    return ExecutionResult("failed", "", 2, 1, message, (message,))


def test_repair_uses_fresh_attempt_and_diagnosis_then_completes() -> None:
    results = deque(
        [
            failed("app.py:10: expected one"),
            failed("app.py:11: expected two"),
            ExecutionResult("completed", "fixed", 2, 2),
        ]
    )
    prompts = []
    diagnoses = []

    def execute(prompt: str) -> ExecutionResult:
        prompts.append(prompt)
        return results.popleft()

    def diagnose(packet: str) -> str:
        diagnoses.append(packet)
        return "Read the assertion and correct the return value."

    outcome = RepairLoop(execute=execute, diagnose=diagnose, max_attempts=4).run("Fix behavior")

    assert outcome.status == "completed"
    assert outcome.attempts == 3
    assert len(diagnoses) == 1
    assert "Fresh-context repair" in prompts[1]
    assert "Diagnosis" in prompts[2]


def test_repair_does_not_retry_external_action_review() -> None:
    calls = []
    outcome = RepairLoop(
        execute=lambda prompt: calls.append(prompt) or ExecutionResult(
            "review_required", "", 1, 0, "unsupported external action requires user review"
        ),
        diagnose=lambda _packet: "should not diagnose", max_attempts=4,
    ).run("Task")
    assert outcome.status == "review_required" and outcome.attempts == 1
    assert len(calls) == 1


def test_repair_stops_on_identical_fingerprint_three_times() -> None:
    outcome = RepairLoop(
        execute=lambda _prompt: failed("app.py:10: same failure"),
        diagnose=lambda _packet: "try again",
        max_attempts=5,
    ).run("Fix behavior")
    assert outcome.status == "escalation_required"
    assert outcome.reason == "stuck"
    assert outcome.attempts == 3


def test_repair_loop_uses_one_external_strategy_after_local_stall() -> None:
    calls = []

    def execute(prompt):
        calls.append(prompt)
        if len(calls) == 4:
            return ExecutionResult("completed", "fixed", 1, 1)
        return ExecutionResult("failed", "", 1, 1, "same error")

    rescues = []
    loop = RepairLoop(
        execute=execute,
        diagnose=lambda _failure: "local diagnosis",
        rescue=lambda packet: rescues.append(packet) or "external strategy",
        max_attempts=4,
    )

    outcome = loop.run("fix it")

    assert outcome.status == "completed"
    assert len(rescues) == 1
    assert "external strategy" in calls[-1]
