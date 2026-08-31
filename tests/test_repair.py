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


def test_repair_stops_on_identical_fingerprint_three_times() -> None:
    outcome = RepairLoop(
        execute=lambda _prompt: failed("app.py:10: same failure"),
        diagnose=lambda _packet: "try again",
        max_attempts=5,
    ).run("Fix behavior")
    assert outcome.status == "escalation_required"
    assert outcome.reason == "stuck"
    assert outcome.attempts == 3

