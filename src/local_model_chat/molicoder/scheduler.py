from __future__ import annotations

from .planner.schema import Plan, PlanTask


class DagScheduler:
    """Deterministic DAG readiness with conservative path-conflict exclusion."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self._started: set[str] = set()
        self._completed: set[str] = set()
        self._failed: set[str] = set()
        self._active: set[str] = set()
        self._observed_paths: dict[str, tuple[str, ...]] = {}

    def _paths(self, task: PlanTask) -> set[str]:
        return set(self._observed_paths.get(task.key, task.expected_paths))

    def ready(self) -> tuple[PlanTask, ...]:
        active_paths: set[str] = set()
        for task in self.plan.tasks:
            if task.key in self._active:
                active_paths.update(self._paths(task))
        ready: list[PlanTask] = []
        for task in self.plan.tasks:
            if task.key in self._started or task.key in self._failed:
                continue
            if not set(task.depends_on) <= self._completed:
                continue
            paths = self._paths(task)
            if paths and active_paths.intersection(paths):
                continue
            ready.append(task)
        return tuple(ready)

    def start(self, key: str) -> None:
        if key not in {task.key for task in self.ready()}:
            raise ValueError(f"task is not ready: {key}")
        self._started.add(key)
        self._active.add(key)

    def complete(self, key: str, changed_paths: tuple[str, ...] = ()) -> None:
        if key not in self._active:
            raise ValueError(f"task is not active: {key}")
        self._active.remove(key)
        self._completed.add(key)
        self._observed_paths[key] = changed_paths

    def fail(self, key: str) -> None:
        self._active.discard(key)
        self._failed.add(key)

    @property
    def complete_keys(self) -> frozenset[str]:
        return frozenset(self._completed)
