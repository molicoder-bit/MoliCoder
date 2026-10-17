from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any


class PlanValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PlanTask:
    key: str
    title: str
    objective: str
    risk: str
    depends_on: tuple[str, ...]
    expected_paths: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ("Configured validation must pass",)
    verification_checks: tuple[str, ...] = ("project-default",)
    artifact_checks: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Plan:
    objective: str
    tasks: tuple[PlanTask, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def ordered_tasks(self) -> tuple[PlanTask, ...]:
        by_key = {task.key: task for task in self.tasks}
        pending = list(self.tasks)
        complete: set[str] = set()
        ordered: list[PlanTask] = []
        while pending:
            ready = [task for task in pending if set(task.depends_on) <= complete]
            if not ready:
                raise PlanValidationError("task dependency cycle detected")
            for task in ready:
                ordered.append(task)
                complete.add(task.key)
                pending.remove(task)
        return tuple(ordered)


def _decode(value: str) -> Any:
    candidate = value.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise PlanValidationError("plan is not valid JSON") from exc


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(f"{field} must be non-empty text")
    return value.strip()


def parse_plan(value: str | dict[str, Any], *, max_tasks: int = 30) -> Plan:
    payload = _decode(value) if isinstance(value, str) else value
    if not isinstance(payload, dict):
        raise PlanValidationError("plan must be an object")
    objective = _required_text(payload.get("objective"), "objective")
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= max_tasks:
        raise PlanValidationError(f"tasks must contain between 1 and {max_tasks} items")
    tasks: list[PlanTask] = []
    keys: set[str] = set()
    for index, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            raise PlanValidationError(f"tasks[{index}] must be an object")
        key = _required_text(item.get("key"), f"tasks[{index}].key")
        if key in keys:
            raise PlanValidationError(f"duplicate task key: {key}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", key):
            raise PlanValidationError(f"invalid task key: {key}")
        keys.add(key)
        risk = _required_text(item.get("risk", "low"), f"tasks[{index}].risk").lower()
        if risk not in {"low", "medium", "high"}:
            raise PlanValidationError(f"invalid task risk: {risk}")
        depends = item.get("depends_on", [])
        if not isinstance(depends, list) or not all(isinstance(dep, str) for dep in depends):
            raise PlanValidationError(f"tasks[{index}].depends_on must be a string list")
        expected_paths = item.get("expected_paths", [])
        if not isinstance(expected_paths, list) or not all(isinstance(path, str) and path.strip() for path in expected_paths):
            raise PlanValidationError(f"tasks[{index}].expected_paths must be a string list")
        criteria = item.get("acceptance_criteria", ["Configured validation must pass"])
        checks = item.get("verification_checks", ["project-default"])
        artifact_checks = item.get("artifact_checks", [])
        if (not isinstance(criteria, list) or not 1 <= len(criteria) <= 5
                or not all(isinstance(value, str) and 1 <= len(value.strip()) <= 300 for value in criteria)):
            raise PlanValidationError(f"tasks[{index}].acceptance_criteria must contain 1 to 5 checkable items")
        if (not isinstance(checks, list) or not 1 <= len(checks) <= 5
                or not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) for value in checks)):
            raise PlanValidationError(f"tasks[{index}].verification_checks must name 1 to 5 approved checks")
        if not isinstance(artifact_checks, list) or len(artifact_checks) > 5:
            raise PlanValidationError(f"tasks[{index}].artifact_checks must contain at most 5 checks")
        for check in artifact_checks:
            if not isinstance(check, dict) or check.get("kind") not in {"file_equals", "directory_empty"}:
                raise PlanValidationError(f"tasks[{index}].artifact_checks contains an unsupported check")
            if (not isinstance(check.get("path"), str) or not check["path"].strip()
                    or len(check["path"]) > 500):
                raise PlanValidationError(f"tasks[{index}].artifact_checks requires a path")
            expected_keys = {"kind", "path", "content"} if check["kind"] == "file_equals" else {"kind", "path"}
            if set(check) != expected_keys or ("content" in check and not isinstance(check["content"], str)):
                raise PlanValidationError(f"tasks[{index}].artifact_checks has invalid fields")
            if "content" in check and len(check["content"]) > 65_536:
                raise PlanValidationError(f"tasks[{index}].artifact_checks content is too large")
        tasks.append(
            PlanTask(
                key,
                _required_text(item.get("title"), f"tasks[{index}].title"),
                _required_text(item.get("objective"), f"tasks[{index}].objective"),
                risk,
                tuple(depends),
                tuple(path.replace("\\", "/").strip() for path in expected_paths),
                tuple(value.strip() for value in criteria),
                tuple(checks),
                tuple(dict(check) for check in artifact_checks),
            )
        )
    for task in tasks:
        missing = set(task.depends_on) - keys
        if missing:
            raise PlanValidationError(f"task {task.key} has missing dependency: {sorted(missing)[0]}")
        if task.key in task.depends_on:
            raise PlanValidationError(f"task {task.key} depends on itself")
    plan = Plan(objective, tuple(tasks))
    plan.ordered_tasks()
    return plan


def validate_provider_schema(schema: dict) -> None:
    """Check the strict object contract before dispatching a provider request."""
    def visit(node):
        if isinstance(node, dict):
            if node.get('type') == 'object':
                if node.get('additionalProperties') is not False:
                    raise PlanValidationError('Provider schema objects must forbid additional properties')
                if set(node.get('required', [])) != set(node.get('properties', {})):
                    raise PlanValidationError('Provider schema must require every declared object property')
            if 'oneOf' in node:
                raise PlanValidationError('Provider schema unions must use anyOf')
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
