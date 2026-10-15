from __future__ import annotations

import re
from dataclasses import dataclass

from .continuity import build_continuity
from .domain import ProjectRunStatus
from .intent import IntentKind, classify_intent
from .storage import ChatStore
from .tool_queries import query_git


@dataclass(frozen=True, slots=True)
class ControllerResult:
    kind: str
    content: str
    evidence: str | None = None
    action_state: str = "answered"


class MainController:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    @staticmethod
    def is_current_task_question(message: str) -> bool:
        return classify_intent(message).operation == "task_status"

    @staticmethod
    def is_deterministic_question(message: str) -> bool:
        decision = classify_intent(message)
        return decision.kind == IntentKind.RETRIEVAL and decision.confidence >= 0.95

    @staticmethod
    def can_handle_locally(message: str) -> bool:
        decision = classify_intent(message)
        return (
            MainController.is_deterministic_question(message)
            or decision.kind == IntentKind.EXECUTION
            and bool(re.match(r"^(?:implement|build)\b", message.strip(), re.IGNORECASE))
        )

    def handle(self, message: str, project_id: int | None) -> ControllerResult | None:
        """Handle local retrieval and Build proposals; return None for normal chat."""
        decision = classify_intent(message)
        if decision.kind == IntentKind.EXECUTION and re.match(
            r"^(?:implement|build)\b", message.strip(), re.IGNORECASE
        ):
            if project_id is None or self.store.get_project(project_id) is None:
                return ControllerResult(
                    "execution_proposal", "Select a project before preparing a Build request.",
                    action_state="awaiting_clarification",
                )
            return ControllerResult(
                "execution_proposal",
                "Build request prepared. Review its project, provider, strategy, and permissions in the Build view before starting it.",
                action_state="awaiting_approval",
            )
        if decision.kind != IntentKind.RETRIEVAL or decision.confidence < 0.95:
            return None
        if project_id is None:
            return ControllerResult(decision.operation or "retrieval", "Select a project to answer this request.")
        if decision.operation == "continuity":
            snapshot = build_continuity(self.store, project_id)
            if snapshot is None:
                return ControllerResult("continuity", "The selected project is no longer available.")
            return ControllerResult(
                "continuity", snapshot.render(),
                evidence=f"SQLite project {snapshot.project_id}; approved decisions and run/task records",
            )
        status = self.store.current_project_run_status(project_id)
        if status is None:
            return ControllerResult(decision.operation or "retrieval", "The selected project is no longer available.")
        if decision.operation in {"git_version", "changed_files"}:
            project = self.store.get_project(project_id)
            assert project is not None
            try:
                result = query_git(project, decision.operation)
            except ValueError as exc:
                return ControllerResult(decision.operation, str(exc), action_state="failed")
            return ControllerResult(decision.operation, result.content, result.evidence)
        return ControllerResult(
            "task_status", self._format_status(status),
            evidence=f"SQLite project {status.project_id}, run {status.run_id}" if status.run_id else f"SQLite project {status.project_id}",
        )

    @staticmethod
    def _format_status(status: ProjectRunStatus) -> str:
        if status.run_id is None:
            return f"**{status.project_name}** has no recorded Build run or current task yet."
        freshness = f" Last updated: {status.updated_at}." if status.updated_at else ""
        heading = f"**{status.project_name} — run #{status.run_id}** is **{status.run_status}**.{freshness}"
        active = [task for task in status.tasks if task.status in {"running", "validating", "repairing", "applying", "in_progress"}]
        if active:
            details = "\n".join(
                f"- Current task **{task.key}** (#{task.id}): {task.title} — **{task.status}**."
                for task in active
            )
        elif status.tasks:
            details = "\n".join(
                f"- {task.key} (#{task.id}): {task.title} — **{task.status}**."
                for task in status.tasks
            )
        else:
            details = "No task records have been created for this run."
        return f"{heading}\n\n{details}\n\nSource: local project/run/task records."
