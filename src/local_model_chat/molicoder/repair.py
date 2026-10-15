from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..inference.fallback import classify_provider_failure
from .executor import ExecutionResult
from .fingerprint import classify_progress, failure_fingerprint


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    category: str
    action: str
    retry_allowed: bool
    fingerprint: str


def classify_task_failure(
    status: str, error: str, *,
    validation_summary: str = "", fingerprints: tuple[str, ...] = (),
    interrupted: bool = False,
) -> RecoveryDecision:
    """Choose a bounded next action from observed failure evidence."""
    fingerprint = failure_fingerprint(error or validation_summary or status)
    if interrupted:
        return RecoveryDecision("interrupted", "reconcile_preserved_worktree", False, fingerprint)
    if error.startswith(("architectural invariant conflict:", "architectural review required:")):
        return RecoveryDecision("architecture", "human_review", False, fingerprint)
    progress = classify_progress((*fingerprints, fingerprint))
    if progress in {"stuck", "oscillating"} or "without making a repository change" in error.lower():
        return RecoveryDecision("no_progress", "stop_repeated_attempt", False, fingerprint)
    if status in {"timeout", "crashed", "process_failed"} or "process exited" in error.lower():
        return RecoveryDecision("agent_process", "restart_from_checkpoint", True, fingerprint)
    provider = classify_provider_failure(status, error)
    if provider.category in {"auth", "quota", "model", "outage"}:
        return RecoveryDecision("provider", provider.action, provider.retry_same_route, fingerprint)
    if validation_summary.startswith("validation failed"):
        return RecoveryDecision("validation", "repair_from_diagnostics", True, fingerprint)
    return RecoveryDecision("unknown", "human_review", False, fingerprint)


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    status: str
    attempts: int
    execution: ExecutionResult
    fingerprints: tuple[str, ...]
    reason: str
    executions: tuple[ExecutionResult, ...] = ()


class RepairLoop:
    def __init__(
        self,
        *,
        execute: Callable[[str], ExecutionResult],
        diagnose: Callable[[str], str],
        rescue: Callable[[str], str] | None = None,
        max_attempts: int = 4,
    ) -> None:
        self.execute = execute
        self.diagnose = diagnose
        self.rescue = rescue
        self.max_attempts = max_attempts

    @staticmethod
    def _failure_text(result: ExecutionResult) -> str:
        return "\n".join((*result.trace[-6:], result.error or result.final_message))[-6000:]

    def run(self, objective: str) -> RepairOutcome:
        fingerprints: list[str] = []
        previous = ""
        diagnosis = ""
        rescue_used = False
        last = ExecutionResult("failed", "", 0, 0, "not started")
        executions: list[ExecutionResult] = []
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
            executions.append(last)
            if last.status == "review_required":
                return RepairOutcome(
                    "review_required", attempt, last, tuple(fingerprints),
                    last.error or "external action requires review", tuple(executions),
                )
            if last.status == "completed":
                return RepairOutcome(
                    "completed", attempt, last, tuple(fingerprints), "", tuple(executions)
                )
            previous = self._failure_text(last)
            fingerprints.append(failure_fingerprint(previous))
            progress = classify_progress(fingerprints)
            if progress in {"stuck", "oscillating"}:
                if self.rescue is not None and not rescue_used and attempt < self.max_attempts:
                    rescue_used = True
                    try:
                        diagnosis = self.rescue(
                            f"Task:\n{objective}\n\nLocal diagnosis:\n{diagnosis}\n\nFailure:\n{previous}"
                        )
                    except Exception:
                        return RepairOutcome(
                            "escalation_required", attempt, last, tuple(fingerprints),
                            progress, tuple(executions)
                        )
                    continue
                return RepairOutcome(
                    "escalation_required", attempt, last, tuple(fingerprints),
                    progress, tuple(executions)
                )
        return RepairOutcome(
            "failed", self.max_attempts, last, tuple(fingerprints),
            "attempt_budget", tuple(executions)
        )
