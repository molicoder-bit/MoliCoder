from __future__ import annotations

import json
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from ..domain import Project
from ..storage import ChatStore
from .planner.base import PlannerProvider
from .recon import Reconnaissance
from .repair import RepairOutcome
from .scheduler import DagScheduler
from .worktree import IntegrationConflict, WorktreeManager


class RepairProvider(Protocol):
    def run(self, objective: str) -> RepairOutcome: ...


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    run_id: int
    status: str
    completed_tasks: int
    total_tasks: int
    error: str = ""
    integration_path: Path | None = None
    integration_branch: str | None = None


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
        self._event_log_lock = threading.Lock()
        if not self.root.is_dir():
            raise ValueError(f"project folder does not exist: {self.root}")

    def _event(self, run_id: int, kind: str, payload: dict, task_id: int | None = None) -> None:
        self.store.add_event(run_id, kind, payload, task_id)
        log_path = self.root / ".molicoder" / "logs" / f"run-{run_id}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "run_id": run_id,
            "task_id": task_id,
            "kind": kind,
            "payload": payload,
        }
        with self._event_log_lock:
            with log_path.open("a", encoding="utf-8", newline="") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
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
        self._event(
            run_id,
            "run_started",
            {
                "request": request,
                "mode": mode,
                "log_path": str(self.root / ".molicoder" / "logs" / f"run-{run_id}.jsonl"),
            },
        )
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


class WorktreeOrchestrator(Phase2Orchestrator):
    """Phase 3 runner: isolated task branches merged into an isolated integration branch."""

    def __init__(
        self,
        *,
        store: ChatStore,
        project: Project,
        planner: PlannerProvider,
        execute_task: Callable[[Path, str], RepairOutcome],
        validate_integration: Callable[[Path], str],
        repair_conflict: Callable[[Path, str], RepairOutcome] | None = None,
        review_integration: Callable[[Path, str], str] | None = None,
        external_sources: tuple[object, ...] = (),
        worker_model: str = "molicoder:latest",
        max_workers: int = 1,
        should_cancel: Callable[[], bool] | None = None,
        wait_if_paused: Callable[[], None] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        worktree_base: str | Path | None = None,
    ) -> None:
        self.store = store
        self.project = project
        self.root = Path(project.path).resolve()
        self.planner = planner
        self.execute_task = execute_task
        self.validate_integration = validate_integration
        self.repair_conflict = repair_conflict
        self.review_integration = review_integration
        self.external_sources = external_sources
        self.worker_model = worker_model
        self.max_workers = max(1, min(4, max_workers))
        self.should_cancel = should_cancel or (lambda: False)
        self.wait_if_paused = wait_if_paused or (lambda: None)
        self.on_event = on_event
        self._event_log_lock = threading.Lock()
        self.worktree_base = worktree_base
        if not self.root.is_dir():
            raise ValueError(f"project folder does not exist: {self.root}")

    def _record_external(self, run_id: int, task_id: int | None = None) -> None:
        for source in (self.planner, *self.external_sources):
            drain = getattr(source, "drain_external_calls", None)
            if not drain:
                continue
            for call in drain():
                self.store.add_external_call(
                    run_id, call.provider, call.purpose, call.reason,
                    call.input_tokens, call.output_tokens, call.seconds, call.status, task_id,
                )
                self._event(run_id, "external_call", asdict(call), task_id)

    def _worker_prompt(
        self,
        request: str,
        plan_objective: str,
        task,
        verified_paths: tuple[str, ...],
    ) -> str:
        expected = "\n".join(f"- {path}" for path in task.expected_paths) or "- Not specified; inspect the repository."
        verified = "\n".join(
            f"- {path}" for path in dict.fromkeys(verified_paths)
        ) or "- No paths were verified during reconnaissance; start with list_dir at the repository root."
        return (
            f"Original development request:\n{request}\n\n"
            f"Validated plan objective:\n{plan_objective}\n\n"
            f"Assigned task: {task.title} ({task.key})\n"
            f"Task objective:\n{task.objective}\n\n"
            f"Verified repository paths (authoritative for files that already exist):\n{verified}\n\n"
            f"Expected paths (unverified planning hints for possible edits/new files):\n{expected}\n\n"
            f"Project type: {self.project.kind}\n"
            "Implement this assigned task completely in the current isolated worktree. "
            "Prefer verified existing paths over a conflicting planning hint. Inspect the existing "
            "files, make concrete edits through the supplied tools, "
            "and finish by running validation. Do not only describe what should be done."
        )

    def _finish_worktree(
        self,
        run_id: int,
        status: str,
        completed: int,
        total: int,
        *,
        error: str = "",
        integration_path: Path | None = None,
        integration_branch: str | None = None,
    ) -> OrchestrationResult:
        self.store.finish_run(run_id, status)
        payload = {
            "status": status,
            "completed_tasks": completed,
            "total_tasks": total,
            "error": error,
            "integration_path": str(integration_path) if integration_path else None,
            "integration_branch": integration_branch,
        }
        event_kind = "run_completed" if status == "completed" else "run_cancelled" if status == "cancelled" else "run_failed"
        self._event(run_id, event_kind, payload)
        run_path = self.root / ".molicoder" / "runs" / f"run-{run_id}.json"
        run_path.write_text(json.dumps(payload | {"run_id": run_id}, indent=2) + "\n", encoding="utf-8", newline="")
        return OrchestrationResult(run_id, status, completed, total, error, integration_path, integration_branch)

    def run(self, request: str, *, mode: str = "LOCAL_ONLY") -> OrchestrationResult:
        run_id = self.store.create_run(self.project.id, request, mode)
        self._event(
            run_id,
            "run_started",
            {
                "request": request,
                "mode": mode,
                "isolation": "git-worktrees",
                "log_path": str(self.root / ".molicoder" / "logs" / f"run-{run_id}.jsonl"),
            },
        )
        if self.should_cancel():
            return self._finish_worktree(run_id, "cancelled", 0, 0, error="cancelled by user")
        self.wait_if_paused()
        try:
            packet = Reconnaissance(self.root).build(request)
            self._event(run_id, "recon_completed", packet.to_dict())
            plan = self.planner.plan(packet)
            self._record_external(run_id)
            self.store.save_plan(run_id, "generated", self.planner.provider, plan.to_dict())
            (self.root / ".molicoder" / "plans" / f"plan-{run_id}.json").write_text(
                json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8", newline=""
            )
            self._event(run_id, "plan_created", {"provider": self.planner.provider, "tasks": len(plan.tasks)})
            manager = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base)
        except Exception as exc:
            return self._finish_worktree(run_id, "failed", 0, 0, error=str(exc))

        task_ids: dict[str, int] = {}
        for task in plan.tasks:
            task_ids[task.key] = self.store.create_run_task(run_id, task.key, task.title, task.objective, task.risk)
        for task in plan.tasks:
            for dependency in task.depends_on:
                self.store.add_task_dependency(task_ids[task.key], task_ids[dependency])

        scheduler = DagScheduler(plan)
        workspaces_by_key = {}
        completed = 0

        def perform_task(task):
            task_id = task_ids[task.key]
            workspace = manager.create_task(task.key)
            self.store.update_task_workspace(
                task_id, worktree=str(workspace.path), branch=workspace.branch, base_commit=workspace.base_commit
            )
            self.store.update_run_task(task_id, "running", 0)
            self._event(run_id, "task_started", asdict(task) | {"worktree": str(workspace.path)}, task_id)
            outcome = self.execute_task(
                workspace.path,
                self._worker_prompt(
                    request,
                    plan.objective,
                    task,
                    packet.relevant_files + packet.architecture,
                ),
            )
            execution = outcome.execution
            executions = outcome.executions or (execution,)
            for attempt, attempted_execution in enumerate(executions, start=1):
                self.store.add_worker_run(
                    task_id, f"executor-attempt-{attempt}", self.worker_model, "ollama",
                    attempted_execution.prompt_tokens, attempted_execution.output_tokens,
                    attempted_execution.seconds, attempted_execution.status,
                )
            failed_executions = executions[:len(outcome.fingerprints)]
            for attempt, (fingerprint, attempted_execution) in enumerate(
                zip(outcome.fingerprints, failed_executions), start=1
            ):
                self.store.add_failure(
                    task_id,
                    fingerprint,
                    {
                        "reason": outcome.reason,
                        "status": outcome.status,
                        "attempt": attempt,
                        "executor_error": attempted_execution.error,
                        "trace": attempted_execution.trace[-12:],
                    },
                )
            self._record_external(run_id, task_id)
            changed = ()
            if outcome.status == "completed":
                changed = manager.commit(workspace, f"MoliCoder task: {task.title}")
                self.store.update_task_workspace(
                    task_id, worktree=str(workspace.path), branch=workspace.branch,
                    base_commit=workspace.base_commit, changed_paths=changed,
                )
            return task, workspace, outcome, changed

        def task_failure_payload(task, workspace, outcome) -> dict:
            execution = outcome.execution
            executions = outcome.executions or (execution,)
            return {
                "key": task.key,
                "title": task.title,
                "reason": outcome.reason,
                "status": outcome.status,
                "attempts": outcome.attempts,
                "executor_status": execution.status,
                "executor_error": execution.error,
                "turns": sum(item.turns for item in executions),
                "tool_calls": sum(item.tool_calls for item in executions),
                "prompt_tokens": sum(item.prompt_tokens for item in executions),
                "output_tokens": sum(item.output_tokens for item in executions),
                "trace": [
                    entry for item in executions for entry in item.trace[-12:]
                ][-48:],
                "worktree": str(workspace.path),
            }

        def task_completed_payload(task, workspace, outcome, changed) -> dict:
            executions = outcome.executions or (outcome.execution,)
            return {
                "key": task.key,
                "title": task.title,
                "status": outcome.status,
                "attempts": outcome.attempts,
                "turns": sum(item.turns for item in executions),
                "tool_calls": sum(item.tool_calls for item in executions),
                "prompt_tokens": sum(item.prompt_tokens for item in executions),
                "output_tokens": sum(item.output_tokens for item in executions),
                "final_message": outcome.execution.final_message[:4000],
                "trace": [
                    entry for item in executions for entry in item.trace[-12:]
                ][-48:],
                "changed_paths": changed,
                "worktree": str(workspace.path),
            }

        failure = ""
        futures = {}
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="molicoder-task") as pool:
            while completed < len(plan.tasks) and not failure:
                self.wait_if_paused()
                if self.should_cancel():
                    failure = "cancelled by user"
                    break
                while len(futures) < self.max_workers:
                    ready = scheduler.ready()
                    if not ready:
                        break
                    task = ready[0]
                    scheduler.start(task.key)
                    futures[pool.submit(perform_task, task)] = task
                if not futures:
                    failure = "no schedulable tasks"
                    break
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    scheduled_task = futures.pop(future)
                    task_id = task_ids[scheduled_task.key]
                    try:
                        task, workspace, outcome, changed = future.result()
                    except Exception as exc:
                        scheduler.fail(scheduled_task.key)
                        self.store.update_run_task(task_id, "failed", 0)
                        failure = str(exc)
                        break
                    workspaces_by_key[task.key] = workspace
                    if outcome.status != "completed":
                        scheduler.fail(task.key)
                        self.store.update_run_task(task_id, outcome.status, outcome.attempts)
                        self._event(
                            run_id, "task_failed",
                            task_failure_payload(task, workspace, outcome), task_id,
                        )
                        failure = outcome.reason or outcome.status
                        if outcome.execution.error:
                            failure += f" ({outcome.execution.error})"
                        break
                    self.store.update_run_task(task_id, "completed", outcome.attempts)
                    scheduler.complete(task.key, changed)
                    completed += 1
                    self._event(
                        run_id, "task_completed",
                        task_completed_payload(task, workspace, outcome, changed), task_id,
                    )
        # A sibling can fail while another parallel worker is still finishing.
        # Drain those results so the database and event log never leave a task
        # falsely marked as running.
        if failure and futures:
            for future, scheduled_task in tuple(futures.items()):
                task_id = task_ids[scheduled_task.key]
                try:
                    task, workspace, outcome, changed = future.result()
                except Exception as exc:
                    scheduler.fail(scheduled_task.key)
                    self.store.update_run_task(task_id, "failed", 0)
                    self._event(
                        run_id, "task_failed",
                        {"key": scheduled_task.key, "reason": str(exc), "status": "failed"},
                        task_id,
                    )
                    continue
                workspaces_by_key[task.key] = workspace
                if outcome.status == "completed":
                    scheduler.complete(task.key, changed)
                    self.store.update_run_task(task_id, "completed", outcome.attempts)
                    completed += 1
                    self._event(
                        run_id, "task_completed",
                        task_completed_payload(task, workspace, outcome, changed), task_id,
                    )
                else:
                    scheduler.fail(task.key)
                    self.store.update_run_task(task_id, outcome.status, outcome.attempts)
                    self._event(
                        run_id, "task_failed",
                        task_failure_payload(task, workspace, outcome), task_id,
                    )
        if failure:
            status = "cancelled" if failure == "cancelled by user" else "failed"
            return self._finish_worktree(run_id, status, completed, len(plan.tasks), error=failure)
        workspaces = [workspaces_by_key[task.key] for task in plan.tasks]

        try:
            self.wait_if_paused()
            if self.should_cancel():
                return self._finish_worktree(run_id, "cancelled", completed, len(plan.tasks), error="cancelled by user")
            integration = manager.create_integration()
            self._event(run_id, "integration_started", {"path": str(integration.path), "branch": integration.branch})
            for workspace in workspaces:
                try:
                    manager.merge_into(integration, workspace, preserve_conflict=True)
                except IntegrationConflict as conflict:
                    self._event(run_id, "integration_conflict", {"paths": conflict.paths, "branch": workspace.branch})
                    if self.repair_conflict is None:
                        raise
                    outcome = self.repair_conflict(
                        integration.path,
                        "Resolve these Git merge conflicts without changing unrelated behavior: " + ", ".join(conflict.paths),
                    )
                    if outcome.status != "completed":
                        raise IntegrationConflict(conflict.paths, outcome.reason)
                    manager.complete_conflict_merge(integration, conflict.paths)
            validation = self.validate_integration(integration.path)
            if not validation.lstrip().startswith("validation passed"):
                return self._finish_worktree(
                    run_id, "failed", completed, len(plan.tasks), error=validation,
                    integration_path=integration.path, integration_branch=integration.branch,
                )
            if self.review_integration is not None:
                try:
                    review = self.review_integration(integration.path, manager.base_commit)
                    self._event(run_id, "integration_reviewed", {"review": review[:12000]})
                except Exception as exc:
                    self._event(run_id, "external_fallback", {"purpose": "review", "reason": str(exc)})
                finally:
                    self._record_external(run_id)
            self._event(run_id, "integration_completed", {"path": str(integration.path), "branch": integration.branch})
            return self._finish_worktree(
                run_id, "completed", completed, len(plan.tasks),
                integration_path=integration.path, integration_branch=integration.branch,
            )
        except Exception as exc:
            return self._finish_worktree(run_id, "failed", completed, len(plan.tasks), error=str(exc))
