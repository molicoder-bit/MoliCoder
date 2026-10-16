from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TaskHandoff:
    packet_version: int
    project_id: int
    run_id: int
    task_id: int
    base_commit: str
    goal: str
    plan_objective: str
    task_key: str
    task_title: str
    task_objective: str
    expected_paths: tuple[str, ...]
    verified_paths: tuple[str, ...]
    approved_constraints: tuple[str, ...]
    failure_evidence: str
    remaining_attempts: int
    acceptance_criteria: tuple[str, ...] = ()
    approved_decision_ids: tuple[int, ...] = ()
    source_revisions: tuple[str, ...] = ()
    verified_work: tuple[str, ...] = ()
    failed_attempt_ids: tuple[int, ...] = ()
    remaining_budget_dollars: float | None = None

    def __post_init__(self) -> None:
        if self.packet_version != 1 or not self.goal.strip() or not self.task_objective.strip():
            raise ValueError("invalid handoff packet")
        if any(Path(path).is_absolute() or ".." in Path(path).parts for path in self.expected_paths):
            raise ValueError("handoff expected path escapes the worktree")
        if self.remaining_attempts < 0:
            raise ValueError("remaining attempts cannot be negative")

    def render(self, *, project_kind: str) -> str:
        verified = "\n".join(f"- {path}" for path in self.verified_paths) or "- none"
        expected = "\n".join(f"- {path}" for path in self.expected_paths) or "- inspect the repository"
        constraints = "\n".join(f"- {item}" for item in self.approved_constraints) or "- none recorded"
        acceptance = "\n".join(f"- {item}" for item in self.acceptance_criteria) or "- task validation profile and expected paths"
        progress = "\n".join(f"- {item}" for item in self.verified_work) or "- none recorded"
        revisions = ", ".join(self.source_revisions) or "none recorded"
        decision_ids = ", ".join(f"#{item}" for item in self.approved_decision_ids) or "none"
        failure = (
            f"\nPrevious validation failed:\n{self.failure_evidence[-8000:]}\n"
            "Correct the failure without reverting valid work.\n"
            if self.failure_evidence else ""
        )
        return (
            f"Handoff packet v{self.packet_version}; project {self.project_id}; run {self.run_id}; task {self.task_id}; "
            f"base {self.base_commit or 'unknown'}; remaining attempts {self.remaining_attempts}.\n"
            f"Original request:\n{self.goal}\n\nPlan objective:\n{self.plan_objective}\n\n"
            f"Assigned task: {self.task_title} ({self.task_key})\n{self.task_objective}\n\n"
            'Execute only the assigned task within its expected changed paths. The original request is context; '
            'other tasks are handled separately.\n\n'
            f"Verified paths:\n{verified}\n\nExpected changed paths:\n{expected}\n"
            f"Acceptance criteria:\n{acceptance}\nVerified prior work:\n{progress}\n"
            f"Approved decision IDs: {decision_ids}. Source revisions: {revisions}.\n"
            f"Approved constraints:\n{constraints}\nProject type: {project_kind}\n{failure}"
        )
