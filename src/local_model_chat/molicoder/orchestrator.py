from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from ..domain import Project
from ..inference.fallback import RouteCooldowns, classify_provider_failure
from ..knowledge import ProjectKnowledge
from ..storage import ChatStore
from ..task_packet import TaskHandoff
from .planner.base import PlannerProvider
from .planner.schema import parse_plan
from .recon import Reconnaissance
from .repair import RepairOutcome, classify_task_failure
from .fingerprint import failure_fingerprint
from .scheduler import DagScheduler
from .validate import assess_architectural_invariants
from .worktree import IntegrationConflict, TaskWorkspace, WorktreeManager
from .providers import FrontierProvider, ProviderResult, ProviderUnavailable


class RepairProvider(Protocol):
    def run(self, objective: str) -> RepairOutcome: ...


def _planner_provider_name(planner: PlannerProvider) -> str:
    provider = getattr(planner, "provider", None)
    name = provider if isinstance(provider, str) else getattr(provider, "name", None)
    if not isinstance(name, str) or not name.strip():
        name = getattr(planner, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Planner has no provider name")
    return name.strip()


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    run_id: int
    status: str
    completed_tasks: int
    total_tasks: int
    error: str = ""
    integration_path: Path | None = None
    integration_branch: str | None = None
    base_commit: str = ""
    final_commit: str = ""


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
            self.store.save_plan(run_id, "generated", _planner_provider_name(self.planner), plan_payload)
            plan_path = self.root / ".molicoder" / "plans" / f"plan-{run_id}.json"
            plan_path.write_text(json.dumps(plan_payload, indent=2) + "\n", encoding="utf-8", newline="")
            self._event(run_id, "plan_created", {"provider": _planner_provider_name(self.planner), "tasks": len(plan.tasks)})
        except Exception as exc:
            return self._finish(run_id, "failed", 0, 0, str(exc))

        task_ids: dict[str, int] = {}
        for task in plan.tasks:
            task_ids[task.key] = self.store.create_run_task(
                run_id, task.key, task.title, task.objective, task.risk,
                acceptance_criteria=task.acceptance_criteria,
                verification_checks=task.verification_checks,
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
            self.store.save_plan(run_id, "generated", _planner_provider_name(self.planner), plan.to_dict())
            (self.root / ".molicoder" / "plans" / f"plan-{run_id}.json").write_text(
                json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8", newline=""
            )
            self._event(run_id, "plan_created", {"provider": _planner_provider_name(self.planner), "tasks": len(plan.tasks)})
            manager = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base)
        except Exception as exc:
            return self._finish_worktree(run_id, "failed", 0, 0, error=str(exc))

        task_ids: dict[str, int] = {}
        for task in plan.tasks:
            task_ids[task.key] = self.store.create_run_task(
                run_id, task.key, task.title, task.objective, task.risk,
                acceptance_criteria=task.acceptance_criteria,
                verification_checks=task.verification_checks,
            )
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


class SequentialRunOrchestrator(Phase2Orchestrator):
    """Codex-first runner using one sequential, checkpointed run worktree."""

    _BLOCKED_LOCAL_TERMS = {
        "auth", "authentication", "authorization", "database", "dependency", "dependencies",
        "flutter", "framework", "install", "manifest", "migration", "package", "payment",
        "permission", "security", "ui", "workflow",
    }
    _BLOCKED_LOCAL_NAMES = {
        "pyproject.toml", "package.json", "package-lock.json", "pubspec.yaml", "pubspec.lock",
        "requirements.txt", "dockerfile", "docker-compose.yml", ".github",
    }
    _PROTECTED_MANIFESTS = {
        "pyproject.toml", "package.json", "package-lock.json", "pubspec.yaml", "pubspec.lock",
        "requirements.txt", "requirements.lock", "poetry.lock", "uv.lock",
    }

    def __init__(
        self,
        *,
        store: ChatStore,
        project: Project,
        planner: PlannerProvider,
        provider: FrontierProvider,
        validate: Callable[[Path], str],
        validate_task: Callable[[Path, object], str] | None = None,
        local_executor: Callable[[Path, str], RepairOutcome] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        wait_if_paused: Callable[[], None] | None = None,
        worktree_base: str | Path | None = None,
        auto_apply: bool = True,
        max_task_attempts: int = 2,
        task_timeout_seconds: float = 1200.0,
        route_decision: dict | None = None,
        inference_gateway: object | None = None,
        model_reviewer: object | None = None,
        alternate_provider: FrontierProvider | None = None,
    ) -> None:
        self.store = store
        self.project = project
        self.root = Path(project.path).resolve()
        self.planner = planner
        self.provider = provider
        self.validate = validate
        self.validate_task = validate_task or (lambda root, _task: validate(root))
        self.local_executor = local_executor
        self.on_event = on_event
        self.should_cancel = should_cancel or (lambda: False)
        self.wait_if_paused = wait_if_paused or (lambda: None)
        self.worktree_base = worktree_base
        self.auto_apply = auto_apply
        self.max_task_attempts = max(1, max_task_attempts)
        self.task_timeout_seconds = task_timeout_seconds
        self.route_decision = route_decision
        self.inference_gateway = inference_gateway
        self.model_reviewer = model_reviewer
        self.alternate_provider = alternate_provider
        self._active_provider = provider
        self._event_log_lock = threading.Lock()
        self._active_run_id: int | None = None
        self._active_task_id: int | None = None
        self._route_cooldowns = RouteCooldowns()
        if hasattr(self.provider, "on_event"):
            self.provider.on_event = self._provider_event
        if self.alternate_provider is not None and hasattr(self.alternate_provider, "on_event"):
            self.alternate_provider.on_event = self._provider_event
        if not self.root.is_dir():
            raise ValueError(f"project folder does not exist: {self.root}")

    def cancel(self) -> None:
        self.provider.cancel()
        if self.alternate_provider is not None:
            self.alternate_provider.cancel()

    def _provider_event(self, kind: str, payload: dict) -> None:
        if self._active_run_id is not None:
            self._event(self._active_run_id, kind, payload, self._active_task_id)
            if kind == "provider_finished" and payload.get("purpose") == "planning":
                self.store.add_external_call(
                    self._active_run_id, str(payload.get("provider") or self.provider.name),
                    "planning", "frontier_plan",
                    int(payload.get("input_tokens") or 0),
                    int(payload.get("output_tokens") or 0),
                    float(payload.get("seconds") or 0),
                    str(payload.get("status") or "unknown"), None,
                )

    @staticmethod
    def normalize_strategy(mode: str) -> str:
        normalized = mode.strip().upper().replace(" ", "_")
        return {
            "AUTO": "HYBRID",
            "FRONTIER_PLAN": "FRONTIER_ONLY",
            "LOCAL_ONLY": "LOCAL_OFFLINE",
        }.get(normalized, normalized)

    def _finish_run(
        self,
        run_id: int,
        status: str,
        completed: int,
        total: int,
        *,
        error: str = "",
        workspace=None,
        final_commit: str = "",
    ) -> OrchestrationResult:
        self.store.update_run_state(
            run_id, status,
            provider=self._active_provider.name,
            model=self._active_provider.model,
            base_commit=workspace.base_commit if workspace else None,
            final_commit=final_commit or None,
            worktree=str(workspace.path) if workspace else None,
            recovery_reason=error if status in {"needs_attention", "ready_to_apply"} else "",
        )
        self.store.finish_run(run_id, status)
        payload = {
            "status": status,
            "completed_tasks": completed,
            "total_tasks": total,
            "error": error,
            "provider": self._active_provider.name,
            "model": self._active_provider.model or None,
            "worktree": str(workspace.path) if workspace else None,
            "branch": workspace.branch if workspace else None,
            "base_commit": workspace.base_commit if workspace else None,
            "final_commit": final_commit or None,
        }
        kind = "run_completed" if status == "completed" else "run_cancelled" if status == "cancelled" else "run_needs_attention" if status in {"needs_attention", "ready_to_apply"} else "run_failed"
        if self.route_decision is not None:
            self._event(run_id, "routing_usage", self.store.run_inference_usage(run_id))
        self._event(run_id, kind, payload)
        run_path = self.root / ".molicoder" / "runs" / f"run-{run_id}.json"
        run_path.write_text(json.dumps(payload | {"run_id": run_id, "log_version": 2}, indent=2) + "\n", encoding="utf-8", newline="")
        return OrchestrationResult(
            run_id, status, completed, total, error,
            Path(workspace.path) if workspace else None,
            workspace.branch if workspace else None,
            workspace.base_commit if workspace else "",
            final_commit,
        )

    def _task_prompt(
        self, request: str, plan, task, packet, validation_error: str = "", *,
        run_id: int = 0, task_id: int = 0, base_commit: str = "", remaining_attempts: int = 0,
    ) -> str:
        decisions = ProjectKnowledge(self.store).approved_decisions(self.project.id)
        constraints = tuple(
            f"Decision #{item.id} r{item.revision}: {item.title} — {item.body} ({item.rationale})"
            for item in decisions[-5:]
        )
        verified_work = tuple(
            f"{item.key} (#{item.id}): {item.title}"
            for item in self.store.list_run_tasks(run_id) if item.status == "completed"
        ) if run_id else ()
        handoff = TaskHandoff(
            1, self.project.id, run_id, task_id, base_commit,
            request, plan.objective, task.key, task.title, task.objective,
            tuple(task.expected_paths),
            tuple(dict.fromkeys(packet.relevant_files + packet.architecture)),
            constraints, validation_error, remaining_attempts,
            acceptance_criteria=task.acceptance_criteria,
            approved_decision_ids=tuple(item.id for item in decisions[-5:]),
            source_revisions=tuple(dict.fromkeys(
                value for value in (base_commit, *(item.source_revision for item in decisions[-5:])) if value
            )),
            verified_work=verified_work,
        )
        return handoff.render(project_kind=self.project.kind)

    def _local_eligible(self, task, workspace: Path) -> bool:
        if task.risk != "low" or not 1 <= len(task.expected_paths) <= 2:
            return False
        terms = set(task.objective.lower().replace("/", " ").replace("-", " ").split())
        if terms & self._BLOCKED_LOCAL_TERMS:
            return False
        for raw in task.expected_paths:
            relative = Path(raw)
            if relative.is_absolute() or ".." in relative.parts or not (workspace / relative).is_file():
                return False
            lowered = {part.lower() for part in relative.parts}
            if lowered & self._BLOCKED_LOCAL_NAMES:
                return False
        return True

    def _changed_paths(self, manager: WorktreeManager, workspace: Path) -> tuple[str, ...]:
        output = manager._git("status", "--porcelain", "--untracked-files=all", cwd=workspace).stdout
        paths: list[str] = []
        for line in output.splitlines():
            raw = line[3:] if len(line) >= 4 else ""
            if " -> " in raw:
                raw = raw.split(" -> ", 1)[1]
            normalized = raw.strip().strip('"').replace("\\", "/")
            if normalized and not normalized.startswith(".molicoder/"):
                paths.append(normalized)
        return tuple(sorted(set(paths)))

    def _scope_error(self, task, manager: WorktreeManager, workspace: Path) -> str:
        changed = self._changed_paths(manager, workspace)
        assessment = assess_architectural_invariants(self.store, self.project.id, changed)
        if assessment.conflicts:
            return "architectural invariant conflict: " + "; ".join(item.message for item in assessment.conflicts)
        if assessment.review_required:
            return "architectural review required: " + "; ".join(assessment.review_required)
        protected = [path for path in changed if Path(path).name.lower() in self._PROTECTED_MANIFESTS]
        if protected:
            return "manifest or dependency change requires explicit approval: " + ", ".join(protected)
        if not task.expected_paths:
            return ""
        expected = tuple(path.strip("/").replace("\\", "/") for path in task.expected_paths)
        unexpected = [
            path for path in changed
            if not any(path == allowed or path.startswith(allowed.rstrip("/") + "/") for allowed in expected)
        ]
        if unexpected:
            return "provider changed paths outside the approved task scope: " + ", ".join(unexpected)
        return ""

    @staticmethod
    def _unmet_criteria(task) -> tuple[str, ...]:
        checks = set(task.verification_checks)
        return tuple(
            criterion for criterion in task.acceptance_criteria
            if criterion != "Configured validation must pass"
            and not (
                criterion.startswith("Check ") and criterion.endswith(" passes")
                and criterion[6:-7] in checks
            )
        )

    def _selective_review(
        self, run_id: int, task_id: int, task, manager: WorktreeManager,
        workspace: Path, checkpoint: str, validation_summary: str,
        unmet: tuple[str, ...],
    ) -> tuple[str, ...]:
        reviewable = tuple(value for value in unmet if value.startswith("Model review:"))
        if not reviewable or self.model_reviewer is None:
            return unmet
        if not validation_summary.lstrip().startswith("validation passed"):
            return unmet
        try:
            diff = manager._git(
                "show", "--format=", "--no-ext-diff", "--no-renames", "HEAD", cwd=workspace,
            ).stdout
            if len(diff) > 20_000:
                raise ValueError("checkpoint diff exceeds the review context cap")
            verdict = self.model_reviewer.review(
                task_key=task.key, task_objective=task.objective, revision=checkpoint,
                diff=diff, validation_summary=validation_summary, criteria=reviewable,
            )
            approved = set(verdict.approved)
            if not approved.issubset(reviewable):
                raise ValueError("model review approved an unrequested criterion")
            self._event(run_id, "model_review_finished", {
                "key": task.key, "revision": checkpoint, "approved": sorted(approved),
                "remaining": [value for value in unmet if value not in approved],
                "reason": verdict.reason[:500],
            }, task_id)
            return tuple(value for value in unmet if value not in approved)
        except Exception as exc:
            self._event(run_id, "model_review_skipped", {
                "key": task.key, "revision": checkpoint, "reason": str(exc)[:500],
            }, task_id)
            return unmet

    def _record_provider_result(
        self, run_id: int, task_id: int, attempt: int, result: ProviderResult,
        provider: FrontierProvider | None = None,
    ) -> None:
        provider = provider or self.provider
        self.store.add_worker_run(
            task_id, f"frontier-attempt-{attempt}", result.model or provider.model or "CLI default",
            provider.name, result.input_tokens, result.output_tokens, result.seconds, result.status,
        )
        self.store.add_external_call(
            run_id, provider.name, "implementation", f"task_attempt_{attempt}",
            result.input_tokens, result.output_tokens, result.seconds, result.status, task_id,
        )

    def fail_unexpected(self, error: str) -> OrchestrationResult:
        run_id = self._active_run_id
        if run_id is None:
            raise RuntimeError(error)
        changed = self.store.fail_active_run(run_id, error)
        tasks = self.store.list_run_tasks(run_id)
        if changed:
            self._event(run_id, "run_needs_attention", {
                "status": "needs_attention", "error": error,
                "completed_tasks": sum(t.status == "completed" for t in tasks),
                "total_tasks": len(tasks),
            }, self._active_task_id)
        run = self.store.get_run(run_id)
        return OrchestrationResult(run_id, run["status"], sum(t.status == "completed" for t in tasks), len(tasks), error,
                                   Path(run["worktree"]) if run.get("worktree") else None)

    def run(self, request: str, *, mode: str = "HYBRID") -> OrchestrationResult:
        try:
            return self._run(request, mode=mode)
        except Exception as exc:
            if self._active_run_id is None:
                raise
            return self.fail_unexpected(str(exc))

    def _run(self, request: str, *, mode: str = "HYBRID") -> OrchestrationResult:
        strategy = self.normalize_strategy(mode)
        run_id = self.store.create_run(
            self.project.id, request, strategy,
            provider=self.provider.name, model=self.provider.model, log_version=2,
        )
        self._active_run_id = run_id
        self.store.record_run_owner(run_id)
        if self.inference_gateway is not None:
            self.inference_gateway.run_id = run_id
            self.inference_gateway.task_id = None
        log_path = self.root / ".molicoder" / "logs" / f"run-{run_id}.jsonl"
        self._event(run_id, "run_started", {
            "request": request, "strategy": strategy, "provider": self.provider.name,
            "model": self.provider.model or None, "isolation": "single-run-worktree",
            "log_path": str(log_path), "log_version": 2,
        })
        if self.route_decision is not None:
            self._event(run_id, "routing_decision", self.route_decision)
        if self.should_cancel():
            return self._finish_run(run_id, "cancelled", 0, 0, error="cancelled by user")
        self._event(run_id, "run_stage", {"stage": "preflight", "message": "Checking repository and provider"})
        self.store.update_run_state(run_id, "preflight")
        try:
            packet = Reconnaissance(self.root).build(request)
            self._event(run_id, "recon_completed", packet.to_dict())
            self._event(run_id, "run_stage", {"stage": "planning", "message": f"{self.provider.name.title()} is creating the plan"})
            self.store.update_run_state(run_id, "planning")
            from .plan_scope import validate_plan_scope
            plan = validate_plan_scope(self.planner.plan(packet), self.root)
            if len(plan.tasks) > 5:
                raise ValueError("plan exceeds the five-task run limit")
            self.store.save_plan(run_id, "generated", _planner_provider_name(self.planner), plan.to_dict())
            (self.root / ".molicoder" / "plans" / f"plan-{run_id}.json").write_text(
                json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8", newline="",
            )
            self._event(run_id, "plan_created", {"provider": _planner_provider_name(self.planner), "tasks": len(plan.tasks)})
            manager = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base)
            workspace = manager.create_run()
            self.store.update_run_state(
                run_id, "running", base_commit=workspace.base_commit,
                worktree=str(workspace.path),
            )
            self._event(run_id, "workspace_created", {
                "worktree": str(workspace.path), "branch": workspace.branch, "base_commit": workspace.base_commit,
            })
        except Exception as exc:
            return self._finish_run(run_id, "needs_attention", 0, 0, error=str(exc))

        task_ids: dict[str, int] = {}
        for task in plan.tasks:
            task_ids[task.key] = self.store.create_run_task(
                run_id, task.key, task.title, task.objective, task.risk,
                acceptance_criteria=task.acceptance_criteria,
                verification_checks=task.verification_checks,
            )
            self.store.update_task_workspace(
                task_ids[task.key], worktree=str(workspace.path), branch=workspace.branch,
                base_commit=workspace.base_commit,
            )
        for task in plan.tasks:
            for dependency in task.depends_on:
                self.store.add_task_dependency(task_ids[task.key], task_ids[dependency])

        completed = 0
        for index, task in enumerate(plan.ordered_tasks(), start=1):
            self._active_provider = self.provider
            self.wait_if_paused()
            if self.should_cancel():
                self.provider.cancel()
                return self._finish_run(run_id, "cancelled", completed, len(plan.tasks), error="cancelled by user", workspace=workspace)
            task_id = task_ids[task.key]
            self._active_task_id = task_id
            if self.inference_gateway is not None:
                self.inference_gateway.task_id = task_id
            self.store.update_run_task(task_id, "ready", 0, reason="dependencies satisfied")
            self.store.update_run_task(task_id, "running", 0)
            self._event(run_id, "task_started", asdict(task) | {
                "task_index": index, "task_total": len(plan.tasks), "worktree": str(workspace.path),
            }, task_id)
            prompt = self._task_prompt(
                request, plan, task, packet, run_id=run_id, task_id=task_id,
                base_commit=workspace.base_commit, remaining_attempts=self.max_task_attempts,
            )
            validation_error = ""
            attempts = 0
            changed: tuple[str, ...] = ()

            local_used = strategy == "HYBRID" and self.local_executor is not None and self._local_eligible(task, workspace.path)
            if strategy == "LOCAL_OFFLINE" and not (self.local_executor and self._local_eligible(task, workspace.path)):
                error = "Local offline mode only supports low-risk edits to one or two existing files."
                self.store.update_run_task(task_id, "blocked", 0, reason=error)
                self._event(run_id, "task_failed", {"key": task.key, "title": task.title, "reason": error, "worktree": str(workspace.path)}, task_id)
                return self._finish_run(run_id, "needs_attention", completed, len(plan.tasks), error=error, workspace=workspace)
            if local_used or strategy == "LOCAL_OFFLINE":
                self._event(run_id, "local_attempt_started", {"key": task.key, "reason": "bounded existing-file edit"}, task_id)
                local_outcome = self.local_executor(workspace.path, prompt)
                attempts = 1
                if local_outcome.status == "review_required":
                    self.store.update_run_task(task_id, "review_required", attempts, reason=local_outcome.reason)
                    self._event(run_id, "task_review_required", {
                        "key": task.key, "reason": local_outcome.reason,
                        "worktree": str(workspace.path),
                    }, task_id)
                    return self._finish_run(
                        run_id, "needs_attention", completed, len(plan.tasks),
                        error=local_outcome.reason, workspace=workspace,
                    )
                if local_outcome.status == "completed":
                    self.store.update_run_task(task_id, "verifying", attempts, reason="checking local task output")
                    self._event(run_id, "validation_started", {"key": task.key, "attempt": attempts}, task_id)
                    validation_error = self._scope_error(task, manager, workspace.path) or self.validate_task(workspace.path, task)
                    if validation_error.lstrip().startswith("validation passed"):
                        try:
                            changed = manager.commit(workspace, f"MoliCoder checkpoint {task.key}: {task.title}")
                        except Exception as exc:
                            validation_error = f"checkpoint failed: {exc}"
                if validation_error.startswith("architectural review required:"):
                    self.store.update_run_task(task_id, "review_required", attempts)
                    self._event(run_id, "task_review_required", {
                        "key": task.key, "reason": validation_error, "worktree": str(workspace.path),
                    }, task_id)
                    return self._finish_run(
                        run_id, "needs_attention", completed, len(plan.tasks),
                        error=validation_error, workspace=workspace,
                    )
                if not changed and strategy == "HYBRID":
                    self._event(run_id, "provider_takeover", {"key": task.key, "provider": self.provider.name, "reason": validation_error or local_outcome.reason}, task_id)
                elif not changed:
                    error = validation_error or local_outcome.reason or "local validation failed"
                    self.store.update_run_task(task_id, "needs_attention", attempts)
                    return self._finish_run(run_id, "needs_attention", completed, len(plan.tasks), error=error, workspace=workspace)

            if not changed:
                wait_state = ""
                opaque_review_required = False
                attempt_provider = self.provider
                failed_fingerprints: set[str] = set()
                for frontier_attempt in range(1, self.max_task_attempts + 1):
                    attempts = frontier_attempt
                    if (local_used and frontier_attempt == 1) or frontier_attempt > 1:
                        try:
                            saved_attempt = manager.preserve_failed_attempt(
                                workspace, f"MoliCoder run {run_id} task {task.key} failed attempt {frontier_attempt - 1}",
                            )
                        except Exception as exc:
                            validation_error = f"failed attempt snapshot: {exc}"
                            break
                        if saved_attempt:
                            self._event(run_id, "failed_attempt_preserved", {
                                "key": task.key, "attempt": frontier_attempt - 1,
                                "stash_commit": saved_attempt,
                                "accepted_checkpoint": manager._git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip(),
                            }, task_id)
                    self.store.update_run_task(
                        task_id, "retrying" if frontier_attempt > 1 else "running", attempts,
                        reason="retrying after failed attempt" if frontier_attempt > 1 else "provider attempt started",
                    )
                    self.wait_if_paused()
                    if self.should_cancel():
                        self.provider.cancel()
                        self.store.update_run_task(task_id, "cancelled", attempts, reason="cancelled by user")
                        return self._finish_run(run_id, "cancelled", completed, len(plan.tasks), error="cancelled by user", workspace=workspace)
                    if not self._route_cooldowns.available(attempt_provider.name):
                        validation_error = "Provider route is cooling down after an outage or quota failure."
                        break
                    self._event(run_id, "provider_attempt_started", {
                        "key": task.key, "provider": attempt_provider.name, "attempt": frontier_attempt,
                        "maximum_attempts": self.max_task_attempts,
                    }, task_id)
                    opaque_attempt = getattr(attempt_provider, "enforcement_class", "") == "opaque_agent"
                    calls_before = getattr(attempt_provider, "calls", None) if opaque_attempt else None
                    try:
                        result = attempt_provider.execute(
                            workspace.path,
                            self._task_prompt(
                                request, plan, task, packet, validation_error,
                                run_id=run_id, task_id=task_id, base_commit=workspace.base_commit,
                                remaining_attempts=self.max_task_attempts - frontier_attempt + 1,
                            ),
                            timeout_seconds=self.task_timeout_seconds,
                        )
                    except (ProviderUnavailable, PermissionError) as exc:
                        validation_error = str(exc)
                        if opaque_attempt and calls_before is not None and getattr(attempt_provider, "calls", None) != calls_before:
                            validation_error = "Opaque agent execution may have caused external side effects: " + validation_error
                            opaque_review_required = True
                            break
                        wait_state = "waiting_approval" if isinstance(exc, PermissionError) else (
                            "waiting_auth" if classify_provider_failure("failed", validation_error).category == "auth" else ""
                        )
                        self._event(run_id, "provider_dispatch_blocked", {
                            "key": task.key, "reason": validation_error,
                        }, task_id)
                        break
                    except Exception as exc:
                        if not opaque_attempt:
                            raise
                        validation_error = "Opaque agent execution may have caused external side effects: " + str(exc)
                        opaque_review_required = True
                        break
                    self._record_provider_result(run_id, task_id, frontier_attempt, result, attempt_provider)
                    if result.status == "cancelled":
                        self.store.update_run_task(task_id, "cancelled", attempts, reason="cancelled by user")
                        return self._finish_run(run_id, "cancelled", completed, len(plan.tasks), error="cancelled by user", workspace=workspace)
                    if result.status != "completed":
                        validation_error = result.error or result.status
                        if opaque_attempt:
                            validation_error = "Opaque agent execution requires review before retry: " + validation_error
                            opaque_review_required = True
                            break
                        action = classify_provider_failure(result.status, validation_error)
                        wait_state = {"auth": "waiting_auth", "quota": "waiting_quota"}.get(action.category, "")
                        self._route_cooldowns.apply(attempt_provider.name, action)
                        fingerprint = failure_fingerprint(validation_error)
                        self._event(run_id, "provider_failure_action", {
                            "key": task.key, "category": action.category,
                            "action": action.action, "retry_same_route": action.retry_same_route,
                        }, task_id)
                        if fingerprint in failed_fingerprints:
                            validation_error = "same provider failure loop stopped: " + validation_error
                            break
                        failed_fingerprints.add(fingerprint)
                        if not action.retry_same_route:
                            if (self.alternate_provider is not None
                                    and attempt_provider is self.provider
                                    and frontier_attempt < self.max_task_attempts):
                                try:
                                    alternate_status = self.alternate_provider.probe()
                                except Exception as exc:
                                    self._event(run_id, "model_handoff_skipped", {
                                        "key": task.key, "reason": str(exc)[:500],
                                    }, task_id)
                                    break
                                if (alternate_status.installed and alternate_status.auth_verified
                                        and alternate_status.authenticated):
                                    checkpoint = manager._git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip()
                                    self._event(run_id, "model_handoff", {
                                        "key": task.key, "from_provider": attempt_provider.name,
                                        "provider": self.alternate_provider.name,
                                        "reason": validation_error[:500], "checkpoint": checkpoint,
                                        "remaining_attempts": self.max_task_attempts - frontier_attempt,
                                        "diagnostic_fingerprint": fingerprint,
                                    }, task_id)
                                    attempt_provider = self.alternate_provider
                                    self._active_provider = attempt_provider
                                    wait_state = ""
                                    continue
                                self._event(run_id, "model_handoff_skipped", {
                                    "key": task.key,
                                    "reason": alternate_status.detail or "fallback authentication is unverified",
                                }, task_id)
                            break
                        if frontier_attempt < self.max_task_attempts:
                            self._event(run_id, "repair_started", {"key": task.key, "reason": validation_error, "attempt": frontier_attempt + 1}, task_id)
                            continue
                        break
                    self.store.update_run_task(task_id, "verifying", attempts, reason="checking provider task output")
                    self._event(run_id, "validation_started", {"key": task.key, "attempt": frontier_attempt}, task_id)
                    validation_error = self._scope_error(task, manager, workspace.path) or self.validate_task(workspace.path, task)
                    passed = validation_error.lstrip().startswith("validation passed")
                    self._event(run_id, "validation_finished", {
                        "key": task.key, "attempt": frontier_attempt, "passed": passed,
                        "summary": validation_error[-8000:],
                    }, task_id)
                    if passed:
                        try:
                            changed = manager.commit(workspace, f"MoliCoder checkpoint {task.key}: {task.title}")
                        except Exception as exc:
                            changed = ()
                            validation_error = f"checkpoint failed: {exc}"
                        if changed:
                            break
                        if not validation_error.startswith("checkpoint failed:"):
                            validation_error = "provider completed without making a repository change"
                    if validation_error.startswith("architectural review required:"):
                        break
                    if opaque_attempt:
                        validation_error = "Opaque agent output requires review before retry: " + validation_error
                        opaque_review_required = True
                        break
                    if frontier_attempt < self.max_task_attempts:
                        self._event(run_id, "repair_started", {"key": task.key, "reason": validation_error, "attempt": frontier_attempt + 1}, task_id)

            if not changed:
                error = validation_error or "provider did not complete the task"
                recovery = classify_task_failure("failed", error, validation_summary=validation_error)
                self._event(run_id, "task_recovery_decision", {
                    "key": task.key, "category": recovery.category,
                    "action": recovery.action, "retry_allowed": recovery.retry_allowed,
                    "fingerprint": recovery.fingerprint,
                }, task_id)
                needs_review = opaque_review_required or error.startswith("architectural review required:")
                if opaque_review_required:
                    self._event(run_id, "opaque_attempt_review_required", {
                        "key": task.key, "attempt": attempts, "provider": attempt_provider.name,
                        "reason": error[:500], "worktree": str(workspace.path),
                    }, task_id)
                final_state = "review_required" if needs_review else wait_state or "needs_attention"
                self.store.update_run_task(task_id, final_state, attempts, reason=error)
                self._event(run_id, "task_review_required" if needs_review else "task_failed", {
                    "key": task.key, "title": task.title, "reason": error,
                    "attempts": attempts, "provider": self._active_provider.name, "worktree": str(workspace.path),
                }, task_id)
                return self._finish_run(run_id, "needs_attention", completed, len(plan.tasks), error=error, workspace=workspace)
            self.store.update_task_workspace(
                task_id, worktree=str(workspace.path), branch=workspace.branch,
                base_commit=workspace.base_commit, changed_paths=changed,
            )
            checkpoint = manager._git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip()
            unmet = self._unmet_criteria(task)
            unmet = self._selective_review(
                run_id, task_id, task, manager, workspace.path, checkpoint, validation_error, unmet,
            )
            completion_record = self.store.record_task_completion(
                run_id, task_id, revision=checkpoint, checks=task.verification_checks,
                validation_summary=validation_error, output_artifacts=changed,
                unmet_criteria=unmet,
                human_review_status="pending" if unmet else "not_required",
            )
            if unmet:
                self.store.update_run_task(task_id, "review_required", attempts)
                self._event(run_id, "task_review_required", {
                    "key": task.key, "checkpoint": checkpoint,
                    "unmet_criteria": list(unmet), "completion_record_at": completion_record.created_at,
                }, task_id)
                return self._finish_run(
                    run_id, "needs_attention", completed, len(plan.tasks),
                    error="Task acceptance criteria require human review", workspace=workspace,
                )
            self.store.update_run_task(task_id, "completed", attempts)
            completed += 1
            self._event(run_id, "task_completed", {
                "key": task.key, "title": task.title, "attempts": attempts,
                "changed_paths": changed, "checkpoint": checkpoint, "worktree": str(workspace.path),
            }, task_id)

        self._event(run_id, "run_stage", {"stage": "validating", "message": "Running final project validation"})
        self.store.update_run_state(run_id, "validating")
        final_validation = self.validate(workspace.path)
        if not final_validation.lstrip().startswith("validation passed"):
            return self._finish_run(
                run_id, "needs_attention", completed, len(plan.tasks),
                error=final_validation, workspace=workspace,
            )
        final_commit = manager._git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip()
        if self.auto_apply:
            self._event(run_id, "run_stage", {"stage": "applying", "message": "Applying validated checkpoints"})
            self.store.update_run_state(run_id, "applying", final_commit=final_commit)
            allowed, reason = manager.can_fast_forward_checkout(workspace)
            if not allowed:
                return self._finish_run(
                    run_id, "ready_to_apply", completed, len(plan.tasks), error=reason,
                    workspace=workspace, final_commit=final_commit,
                )
            try:
                final_commit = manager.fast_forward_checkout(workspace)
            except Exception as exc:
                return self._finish_run(
                    run_id, "ready_to_apply", completed, len(plan.tasks), error=str(exc),
                    workspace=workspace, final_commit=final_commit,
                )
            self._event(run_id, "changes_applied", {
                "branch": workspace.branch, "base_commit": workspace.base_commit, "final_commit": final_commit,
            })
        return self._finish_run(
            run_id, "completed", completed, len(plan.tasks), workspace=workspace, final_commit=final_commit,
        )

    def resume(self, run_id: int) -> OrchestrationResult:
        """Resume a preserved run worktree with a fresh provider context."""
        run = self.store.get_run(run_id)
        if run is None or int(run["project_id"]) != self.project.id:
            raise ValueError("run does not belong to the selected project")
        if run["status"] not in {"needs_attention", "ready_to_apply", "cancelled", "failed"}:
            raise ValueError(f"run cannot be resumed from status {run['status']}")
        path = Path(str(run.get("worktree") or "")).resolve()
        if not path.is_dir():
            raise ValueError("the preserved run worktree is missing")
        branch_result = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        branch = branch_result.stdout.strip()
        if not branch.startswith("molicoder/run-"):
            raise ValueError("the preserved folder is not a MoliCoder run branch")
        plan_payload = self.store.get_run_plan(run_id)
        if not plan_payload:
            raise ValueError("the saved plan is missing")
        plan = parse_plan(plan_payload)
        packet = Reconnaissance(path).build(str(run["request"]))
        workspace = TaskWorkspace("workspace", path, branch, str(run.get("base_commit") or ""))
        manager = WorktreeManager(self.root, run_id=run_id, base_dir=path.parent.parent)
        self._active_run_id = run_id
        self.store.record_run_owner(run_id)
        if self.inference_gateway is not None:
            self.inference_gateway.run_id = run_id
        tasks = self.store.list_run_tasks(run_id)
        target = next((task for task in tasks if task.status != "completed"), tasks[-1] if tasks else None)
        if target is None:
            raise ValueError("the saved run has no tasks")
        self._active_task_id = target.id
        if self.inference_gateway is not None:
            self.inference_gateway.task_id = target.id
        self.store.reopen_run(run_id)
        self._event(run_id, "run_resumed", {
            "provider": self.provider.name, "model": self.provider.model or None,
            "worktree": str(path), "previous_status": run["status"],
        }, target.id)
        validation_error = str(run.get("recovery_reason") or "Resume the incomplete validated plan.")
        for attempt in range(1, self.max_task_attempts + 1):
            if self.should_cancel():
                return self._finish_run(run_id, "cancelled", 0, len(plan.tasks), error="cancelled by user", workspace=workspace)
            prompt = (
                f"Resume this interrupted MoliCoder run in its preserved worktree.\n\n"
                f"Original request:\n{run['request']}\n\nSaved plan:\n{json.dumps(plan_payload, indent=2)}\n\n"
                f"Current recovery reason or validation output:\n{validation_error[-10000:]}\n\n"
                "Inspect the existing checkpoint and dirty changes, finish every incomplete plan task, and correct validation failures."
            )
            result = self.provider.execute(path, prompt, timeout_seconds=self.task_timeout_seconds)
            self._record_provider_result(run_id, target.id, attempt, result)
            if result.status == "cancelled":
                return self._finish_run(run_id, "cancelled", 0, len(plan.tasks), error="cancelled by user", workspace=workspace)
            if result.status != "completed":
                validation_error = result.error or result.status
                continue
            self._event(run_id, "validation_started", {"key": target.key, "attempt": attempt, "resume": True}, target.id)
            validation_error = self.validate(path)
            passed = validation_error.lstrip().startswith("validation passed")
            self._event(run_id, "validation_finished", {
                "key": target.key, "attempt": attempt, "passed": passed,
                "summary": validation_error[-8000:], "resume": True,
            }, target.id)
            if passed:
                changed = manager.commit(workspace, f"MoliCoder resumed run {run_id}")
                final_commit = manager._git("rev-parse", "HEAD", cwd=path).stdout.strip()
                unfinished = [task for task in tasks if task.status != "completed"]
                unmet = self._unmet_criteria(target)
                if not changed and not target.changed_paths:
                    unmet += ("No changed artifacts are attributed to this task",)
                if len(unfinished) > 1:
                    unmet += ("Multiple unfinished tasks need separate verification",)
                if len(unfinished) == 1:
                    unmet = self._selective_review(
                        run_id, target.id, target, manager, path, final_commit,
                        validation_error, unmet,
                    )
                self.store.record_task_completion(
                    run_id, target.id, revision=final_commit,
                    checks=target.verification_checks, validation_summary=validation_error,
                    output_artifacts=changed or target.changed_paths,
                    unmet_criteria=unmet,
                    human_review_status="pending" if unmet else "not_required",
                )
                if unmet:
                    self.store.update_run_task(target.id, "review_required", max(target.attempts, attempt))
                    self._event(run_id, "task_review_required", {
                        "key": target.key, "checkpoint": final_commit,
                        "unmet_criteria": list(unmet), "resume": True,
                    }, target.id)
                    return self._finish_run(
                        run_id, "needs_attention", len(tasks) - len(unfinished), len(plan.tasks),
                        error="Resumed task acceptance requires human review", workspace=workspace,
                    )
                self.store.update_run_task(target.id, "completed", max(target.attempts, attempt))
                allowed, reason = manager.can_fast_forward_checkout(workspace)
                if self.auto_apply and allowed:
                    final_commit = manager.fast_forward_checkout(workspace)
                    self._event(run_id, "changes_applied", {
                        "branch": branch, "base_commit": workspace.base_commit,
                        "final_commit": final_commit, "resumed": True,
                    })
                    return self._finish_run(
                        run_id, "completed", len(plan.tasks), len(plan.tasks),
                        workspace=workspace, final_commit=final_commit,
                    )
                return self._finish_run(
                    run_id, "ready_to_apply", len(plan.tasks), len(plan.tasks),
                    error=reason or "automatic apply is disabled", workspace=workspace,
                    final_commit=final_commit,
                )
            if attempt < self.max_task_attempts:
                self._event(run_id, "repair_started", {
                    "key": target.key, "attempt": attempt + 1, "reason": validation_error,
                    "resume": True,
                }, target.id)
        return self._finish_run(
            run_id, "needs_attention", sum(task.status == "completed" for task in tasks), len(plan.tasks),
            error=validation_error, workspace=workspace,
        )
