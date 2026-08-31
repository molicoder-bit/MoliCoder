from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .executor import ExecutionResult
from .fingerprint import classify_progress, failure_fingerprint


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    status: str
    attempts: int
    execution: ExecutionResult
    fingerprints: tuple[str, ...]
    reason: str


class RepairLoop:
    def __init__(
        self,
        *,
        execute: Callable[[str], ExecutionResult],
        diagnose: Callable[[str], str],
        max_attempts: int = 4,
    ) -> None:
        self.execute = execute
        self.diagnose = diagnose
        self.max_attempts = max_attempts

    @staticmethod
    def _failure_text(result: ExecutionResult) -> str:
        return "\n".join((*result.trace[-6:], result.error or result.final_message))[-6000:]

    def run(self, objective: str) -> RepairOutcome:
        fingerprints: list[str] = []
        previous = ""
        diagnosis = ""
        last = ExecutionResult("failed", "", 0, 0, "not started")
        for attempt in range(1, self.max_attempts + 1):
            if attempt == 1:
                prompt = objective
            elif attempt == 2:
                prompt = f"{objective}\n\nFresh-context repair. Previous failure:\n{previous}"
            else:
                if not diagnosis:
                    diagnosis = self.diagnose(
                        f"Task:\n{objective}\n\nAttempts and current failure:\n{previous}"
                    )
                prompt = (
                    f"{objective}\n\nFresh-context implementation.\nDiagnosis:\n{diagnosis}\n\n"
                    f"Previous failure:\n{previous}"
                )
            last = self.execute(prompt)
            if last.status == "completed":
                return RepairOutcome("completed", attempt, last, tuple(fingerprints), "")
            previous = self._failure_text(last)
            fingerprints.append(failure_fingerprint(previous))
            progress = classify_progress(fingerprints)
            if progress in {"stuck", "oscillating"}:
                return RepairOutcome("escalation_required", attempt, last, tuple(fingerprints), progress)
        return RepairOutcome("failed", self.max_attempts, last, tuple(fingerprints), "attempt_budget")
