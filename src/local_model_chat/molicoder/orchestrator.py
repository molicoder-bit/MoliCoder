from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..domain import Project
from ..storage import ChatStore
from .planner.base import PlannerProvider
from .recon import Reconnaissance
from .repair import RepairOutcome


class RepairProvider(Protocol):
    def run(self, objective: str) -> RepairOutcome: ...


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    run_id: int
    status: str
    completed_tasks: int
    total_tasks: int
    error: str = ""


class Phase2Orchestrator:
    def __init__(
        self,
        *,
        store: ChatStore,
        project: Project,
        planner: PlannerProvider,
        repair: RepairProvider,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.store = store
        self.project = project
        self.root = Path(project.path).resolve()
        self.planner = planner
        self.repair = repair
        self.on_event = on_event
        if not self.root.is_dir():
            raise ValueError(f"project folder does not exist: {self.root}")

    def _event(self, run_id: int, kind: str, payload: dict, task_id: int | None = None) -> None:
        self.store.add_event(run_id, kind, payload, task_id)
        if self.on_event:
            self.on_event(kind, payload)

    def _finish(self, run_id: int, status: str, completed: int, total: int, error: str = "") -> OrchestrationResult:
        self.store.finish_run(run_id, status)
        payload = {"status": status, "completed_tasks": completed, "total_tasks": total, "error": error}
        self._event(run_id, "run_completed" if status == "completed" else "run_failed", payload)
        run_path = self.root / ".molicoder" / "runs" / f"run-{run_id}.json"
        run_path.write_text(json.dumps(payload | {"run_id": run_id}, indent=2) + "\n", encoding="utf-8", newline="")
        return OrchestrationResult(run_id, status, completed, total, error)

    def run(self, request: str, *, mode: str = "LOCAL_ONLY") -> OrchestrationResult:
        run_id = self.store.create_run(self.project.id, request, mode)
        self._event(run_id, "run_started", {"request": request, "mode": mode})
        try:
            packet = Reconnaissance(self.root).build(request)
            self._event(run_id, "recon_completed", packet.to_dict())
            plan = self.planner.plan(packet)
            plan_payload = plan.to_dict()
            self.store.save_plan(run_id, "generated", self.planner.provider, plan_payload)
            plan_path = self.root / ".molicoder" / "plans" / f"plan-{run_id}.json"
            plan_path.write_text(json.dumps(plan_payload, indent=2) + "\n", encoding="utf-8", newline="")
            self._event(run_id, "plan_created", {"provider": self.planner.provider, "tasks": len(plan.tasks)})
        except Exception as exc:
            return self._finish(run_id, "failed", 0, 0, str(exc))

        task_ids: dict[str, int] = {}
        for task in plan.tasks:
            task_ids[task.key] = self.store.create_run_task(
                run_id, task.key, task.title, task.objective, task.risk
            )
        for task in plan.tasks:
            for dependency in task.depends_on:
                self.store.add_task_dependency(task_ids[task.key], task_ids[dependency])

        completed = 0
        for task in plan.ordered_tasks():
            task_id = task_ids[task.key]
            task_payload = asdict(task)
            task_path = self.root / ".molicoder" / "tasks" / f"task-{task.key}.json"
            task_path.write_text(json.dumps(task_payload, indent=2) + "\n", encoding="utf-8", newline="")
            self.store.update_run_task(task_id, "running", 0)
            self._event(run_id, "task_started", task_payload, task_id)
            outcome = self.repair.run(task.objective)
            task_status = "completed" if outcome.status == "completed" else outcome.status
            self.store.update_run_task(task_id, task_status, outcome.attempts)
            self._event(
                run_id,
                "task_completed" if outcome.status == "completed" else "task_failed",
                {"key": task.key, "status": outcome.status, "attempts": outcome.attempts, "reason": outcome.reason},
                task_id,
            )
            if outcome.status != "completed":
                return self._finish(run_id, "failed", completed, len(plan.tasks), outcome.reason)
            completed += 1
        return self._finish(run_id, "completed", completed, len(plan.tasks))
