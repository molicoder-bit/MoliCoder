from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
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
from .plan_approval import plan_digest, require_approved_plan
from .plan_review import plan_review_warnings
from .artifact_checks import verify_artifact_checks
from .intake import clarification_needed
from .recon import Reconnaissance
from .repair import RepairOutcome, classify_task_failure
from .fingerprint import classify_progress, failure_fingerprint
from .scheduler import DagScheduler
from .validate import assess_architectural_invariants
from .worktree import IntegrationConflict, TaskWorkspace, WorktreeManager
from .providers import FrontierProvider, ProviderResult, ProviderUnavailable, redact_text


def _redact_event_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_event_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_event_value(item) for item in value]
    return value


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
        payload = _redact_event_value(payload)
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
        validate_task: Callable[[Path, object], str] | None = None,
        repair_conflict: Callable[[Path, str], RepairOutcome] | None = None,
        review_integration: Callable[[Path, str], str] | None = None,
        external_sources: tuple[object, ...] = (),
        worker_model: str = "molicoder:latest",
        max_workers: int = 1,
        should_cancel: Callable[[], bool] | None = None,
        wait_if_paused: Callable[[], None] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        worktree_base: str | Path | None = None,
        require_plan_approval: bool = False,
    ) -> None:
        self.store = store
        self.project = project
        self.root = Path(project.path).resolve()
        self.planner = planner
        self.execute_task = execute_task
        self.validate_integration = validate_integration
        self.validate_task = validate_task
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
        self.require_plan_approval = require_plan_approval
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
        return self._run(request, mode=mode)

    def execute_approved(self, run_id: int) -> OrchestrationResult:
        if not self.require_plan_approval:
            raise ValueError("Worktree plan approval is not enabled")
        run, plan = require_approved_plan(self.store, run_id)
        if int(run["project_id"]) != self.project.id or run["status"] != "awaiting_approval":
            raise PermissionError("Worktree Build is not awaiting approval for this project")
        return self._run(str(run["request"]), mode=str(run["mode"]), approved_run_id=run_id)

    def _run(self, request: str, *, mode: str, approved_run_id: int | None = None) -> OrchestrationResult:
        run_id = approved_run_id or self.store.create_run(self.project.id, request, mode)
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
            if approved_run_id is None:
                plan = self.planner.plan(packet)
                if self.require_plan_approval:
                    from .plan_scope import validate_plan_scope
                    plan = validate_plan_scope(plan, self.root)
                if not 1 <= len(plan.tasks) <= 20:
                    raise ValueError("parallel worktree plans require between 1 and 20 tasks")
                plan.ordered_tasks()
                self._record_external(run_id)
                payload = plan.to_dict()
                if self.require_plan_approval:
                    payload["runner_policy"] = {
                        "backend": "worktree", "worker_model": self.worker_model,
                        "max_workers": self.max_workers,
                    }
                revision = self.store.save_plan(run_id, "generated", _planner_provider_name(self.planner), payload)
                (self.root / ".molicoder" / "plans" / f"plan-{run_id}.json").write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline=""
                )
                self._event(run_id, "plan_created", {"provider": _planner_provider_name(self.planner), "tasks": len(plan.tasks)})
                if self.require_plan_approval:
                    base = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base).base_commit
                    self.store.update_run_state(run_id, "awaiting_approval", base_commit=base)
                    self._event(run_id, "plan_awaiting_approval", {
                        "revision": revision, "base_commit": base,
                        "task_keys": [task.key for task in plan.tasks],
                    })
                    return OrchestrationResult(run_id, "awaiting_approval", 0, len(plan.tasks))
            else:
                approved_run, saved = require_approved_plan(self.store, run_id)
                approved_plan_id = int(saved["id"])
                approved_plan_digest = plan_digest(saved["payload"])
                if str(approved_run["request"]) != request or str(approved_run["mode"]) != mode:
                    raise PermissionError("Approved worktree Build request changed")
                if saved["payload"].get("runner_policy") != {
                    "backend": "worktree", "worker_model": self.worker_model,
                    "max_workers": self.max_workers,
                }:
                    raise PermissionError("Approved worktree runner policy changed")
                from .plan_scope import validate_plan_scope
                plan = validate_plan_scope(parse_plan(saved["payload"], max_tasks=20), self.root)
            manager = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base)
            if approved_run_id is not None and manager.base_commit != approved_run["base_commit"]:
                raise PermissionError("Project HEAD changed after worktree plan approval")
            self.store.update_run_state(run_id, "running", base_commit=manager.base_commit)
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
            for dependency in task.depends_on:
                manager.merge_into(workspace, workspaces_by_key[dependency])
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
            if outcome.status == "completed" and self.validate_task is not None:
                validation = self.validate_task(workspace.path, task)
                if not validation.lstrip().startswith("validation passed"):
                    outcome = replace(outcome, status="failed", reason=validation)
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
                if self.require_plan_approval and any(
                    not any(path == scope or path.startswith(scope.rstrip("/") + "/")
                            for scope in task.expected_paths)
                    for path in changed
                ):
                    raise PermissionError(f"task {task.key} changed paths outside its approved file scope")
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

        def persist_verified_completion(task, workspace, changed) -> None:
            if self.validate_task is None:
                return
            self.store.record_task_completion(
                run_id, task_ids[task.key],
                revision=manager._git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip(),
                checks=task.verification_checks,
                validation_summary="validation passed by configured task validator",
                output_artifacts=changed,
                unmet_criteria=(), human_review_status="not_required",
            )

        failure = ""
        futures = {}

        def approval_current() -> bool:
            if approved_run_id is None:
                return True
            try:
                _, current = require_approved_plan(self.store, run_id)
            except PermissionError:
                return False
            return int(current["id"]) == approved_plan_id and plan_digest(current["payload"]) == approved_plan_digest

        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="molicoder-task") as pool:
            while completed < len(plan.tasks) and not failure:
                self.wait_if_paused()
                if self.should_cancel():
                    failure = "cancelled by user"
                    break
                while len(futures) < self.max_workers:
                    if not approval_current():
                        failure = "plan approval changed during parallel execution"
                        break
                    ready = scheduler.ready()
                    if not ready:
                        break
                    task = ready[0]
                    scheduler.start(task.key)
                    futures[pool.submit(perform_task, task)] = task
                if not futures:
                    failure = failure or "no schedulable tasks"
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
                    persist_verified_completion(task, workspace, changed)
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
                    persist_verified_completion(task, workspace, changed)
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
        if not failure and not approval_current():
            failure = "plan approval changed before integration"
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
            if not approval_current():
                return self._finish_worktree(
                    run_id, "failed", completed, len(plan.tasks),
                    error="plan approval changed before integration acceptance",
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
        require_plan_approval: bool = False,
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
        self.require_plan_approval = require_plan_approval
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
        error = redact_text(error)
        self.store.update_run_state(
            run_id, status,
            provider=self._active_provider.name,
            model=self._active_provider.model,
            base_commit=workspace.base_commit if workspace else None,
            final_commit=final_commit or None,
            worktree=str(workspace.path) if workspace else None,
            recovery_reason=error if status != "completed" else "",
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
        rendered = handoff.render(project_kind=self.project.kind)
        if task.artifact_checks:
            rendered += "\n\nApproved exact artifact checks:\n" + json.dumps(
                task.artifact_checks, ensure_ascii=False, indent=2,
            )
        return rendered

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

    def _runner_policy(self, strategy: str) -> dict:
        return {
            "backend": "internal", "strategy": strategy,
            "provider": self.provider.name, "model": self.provider.model,
            "local_model": "molicoder:latest" if self.local_executor else None,
            "fallback_provider": self.alternate_provider.name if self.alternate_provider else None,
            "fallback_model": self.alternate_provider.model if self.alternate_provider else None,
            "selection_reason": "local eligible tasks use the local executor in Hybrid mode; otherwise the selected provider",
            "auto_apply": self.auto_apply,
            "max_task_attempts": self.max_task_attempts,
        }

    def _provider_for_model(self, model: str) -> FrontierProvider:
        if model and model == self.provider.model:
            return self.provider
        if self.alternate_provider is not None and model and model == self.alternate_provider.model:
            return self.alternate_provider
        raise ValueError(f"Model {model!r} is not a configured Build provider model")

    def _task_assignments(self, plan, strategy: str, model_overrides: dict[str, str] | None = None) -> dict[str, dict]:
        model_overrides = model_overrides or {}
        unknown = set(model_overrides) - {task.key for task in plan.tasks}
        if unknown:
            raise ValueError(f"Model override references unknown task: {sorted(unknown)[0]}")
        assignments = {}
        for task in plan.tasks:
            override = model_overrides.get(task.key)
            selected_provider = self._provider_for_model(override) if override else self.provider
            local = override is None and self.local_executor is not None and self._local_eligible(task, self.root)
            if strategy == "LOCAL_OFFLINE" and override:
                raise ValueError("Local offline Build cannot use a provider model override")
            if strategy == "LOCAL_OFFLINE":
                runner = "local" if local else "unavailable"
            elif strategy == "HYBRID" and local:
                runner = "local"
            else:
                runner = "provider_cli"
            assignments[task.key] = {
                "runner": runner,
                "provider": "ollama" if runner == "local" else selected_provider.name if runner == "provider_cli" else None,
                "model": "molicoder:latest" if runner == "local" else selected_provider.model if runner == "provider_cli" else None,
                "fallback_runner": "provider_cli" if runner == "local" and strategy == "HYBRID" else None,
                "reason": "eligible bounded edit to existing files" if runner == "local" else
                          "local route cannot satisfy task capability" if runner == "unavailable" else
                          "user-selected compatible model" if override else "selected coding provider",
            }
        return assignments

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

    def _task_validation(self, task, manager: WorktreeManager, workspace: Path) -> str:
        result = self._scope_error(task, manager, workspace) or self.validate_task(workspace, task)
        if result.lstrip().startswith("validation passed"):
            failures = verify_artifact_checks(workspace, task.artifact_checks)
            if failures:
                return "artifact validation failed: " + "; ".join(failures)
        return result

    @staticmethod
    def _unmet_criteria(task) -> tuple[str, ...]:
        checks = set(task.verification_checks)
        return tuple(
            criterion for criterion in task.acceptance_criteria
            if criterion != "Configured validation must pass"
            and not (criterion == "Artifact checks must pass" and task.artifact_checks)
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
        error = redact_text(error)
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

    def execute_approved(self, run_id: int) -> OrchestrationResult:
        """Continue only the latest plan revision explicitly approved for this run."""
        run, _plan = require_approved_plan(self.store, run_id)
        if int(run["project_id"]) != self.project.id or run["status"] != "awaiting_approval":
            raise PermissionError("Build is not awaiting approval for this project")
        try:
            return self._run(str(run["request"]), mode=str(run["mode"]), approved_run_id=run_id)
        except Exception as exc:
            if self._active_run_id is None:
                raise
            return self.fail_unexpected(str(exc))

    def revise_plan(self, run_id: int, expected_revision: int, edited: str | dict) -> int:
        """Save a new unapproved plan revision after validating task scope and DAG."""
        if not self.require_plan_approval:
            raise ValueError("Plan revision requires the approval-gated Build workflow")
        run = self.store.get_run(run_id)
        latest = self.store.get_latest_plan_record(run_id)
        if (run is None or latest is None or int(run["project_id"]) != self.project.id
                or run["status"] != "awaiting_approval"):
            raise ValueError("Only a pending plan for this project can be revised")
        if int(latest["id"]) != expected_revision:
            raise ValueError("Plan revision changed while editing; reload the latest plan")
        if (str(run["provider"]) != self.provider.name
                or str(run["model"]) != self.provider.model):
            raise ValueError("Build route changed; restore the planned provider before editing")
        current_head = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base)._git(
            "rev-parse", "HEAD", cwd=self.root,
        ).stdout.strip()
        if current_head != run["base_commit"]:
            raise ValueError("Project HEAD changed; create a new plan for the new base")
        raw = json.loads(edited) if isinstance(edited, str) else edited
        if not isinstance(raw, dict):
            raise ValueError("Edited plan must be a JSON object")
        extra = set(raw) - {"objective", "tasks", "task_model_overrides"}
        if extra:
            raise ValueError(
                "Plan editor cannot override runner or model assignments directly; use task_model_overrides"
                if extra & {"runner_policy", "task_assignments", "runner", "model"}
                else "Plan editor accepts only objective, tasks, and task_model_overrides"
            )
        allowed_task_fields = {
            "key", "title", "objective", "risk", "depends_on", "expected_paths",
            "acceptance_criteria", "verification_checks", "artifact_checks",
        }
        for item in raw.get("tasks", []) if isinstance(raw.get("tasks"), list) else []:
            if isinstance(item, dict) and set(item) - allowed_task_fields:
                raise ValueError("Plan task has unsupported fields; runner and model overrides are unavailable")
        from .plan_scope import validate_plan_scope
        plan = validate_plan_scope(parse_plan(raw), self.root)
        model_overrides = raw.get("task_model_overrides", {})
        if (not isinstance(model_overrides, dict)
                or not all(isinstance(key, str) and isinstance(model, str) and model.strip()
                           for key, model in model_overrides.items())):
            raise ValueError("task_model_overrides must map task keys to configured model names")
        unknown = set(model_overrides) - {task.key for task in plan.tasks}
        if unknown:
            raise ValueError(f"Model override references unknown task: {sorted(unknown)[0]}")
        for model in model_overrides.values():
            selected = self._provider_for_model(model)
            if selected is not self.provider:
                status = selected.probe()
                if not (status.installed and status.auth_verified and status.authenticated):
                    raise ValueError(f"Model {model!r} is not ready for Build execution")
        if run.get("worktree"):
            previous = parse_plan(latest["payload"])
            recorded = {task.key: task for task in self.store.list_run_tasks(run_id)}
            if plan.objective != previous.objective or [task.key for task in plan.tasks] != [
                task.key for task in previous.tasks
            ]:
                raise ValueError("A paused repair can change file scope only; task graph and objective are fixed")
            for before, after in zip(previous.tasks, plan.tasks):
                saved_task = recorded.get(before.key)
                if saved_task is None:
                    raise ValueError("A paused repair task is missing from persisted state")
                if saved_task.status == "completed":
                    if after != before:
                        raise ValueError("A completed task cannot be changed in a repair revision")
                elif (replace(after, expected_paths=before.expected_paths) != before
                      or not set(before.expected_paths) <= set(after.expected_paths)):
                    raise ValueError("A paused repair can only expand an unfinished task's file scope")
        strategy = self.normalize_strategy(str(run["mode"]))
        payload = plan.to_dict() | {
            "runner_policy": self._runner_policy(strategy),
            "task_assignments": self._task_assignments(plan, strategy, model_overrides),
            "task_model_overrides": model_overrides,
            "review_warnings": plan_review_warnings(plan),
        }
        revision = self.store.save_plan(
            run_id, "user_revised", self.provider.name, payload,
            expected_latest_id=expected_revision,
        )
        self._event(run_id, "plan_revised", {
            "previous_revision": expected_revision, "revision": revision,
            "digest": plan_digest(payload), "task_keys": [task.key for task in plan.tasks],
        })
        plan_path = self.root / ".molicoder" / "plans" / f"plan-{run_id}.json"
        try:
            plan_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="")
        except OSError as exc:
            self._event(run_id, "plan_export_failed", {"revision": revision, "reason": str(exc)[:500]})
        return revision

    def _propose_scope_revision(
        self, run_id: int, plan, task, task_id: int, attempts: int,
        completed: int, manager: WorktreeManager, workspace: TaskWorkspace,
        approved_plan_id: int, strategy: str, error: str,
    ) -> OrchestrationResult | None:
        """Preserve a failed attempt and request approval for its extra file scope."""
        from .plan_scope import validate_plan_scope

        actual = self._changed_paths(manager, workspace.path)
        allowed = tuple(path.strip("/").replace("\\", "/") for path in task.expected_paths)
        extra = tuple(path for path in actual if not any(
            path == scope or path.startswith(scope.rstrip("/") + "/") for scope in allowed
        ))
        if not extra or len(extra) > 20:
            return None
        workspace_root = workspace.path.resolve()
        if any(not (workspace_root / path).resolve().is_relative_to(workspace_root) for path in extra):
            raise ValueError("repair output escapes the isolated worktree")
        revised_task = replace(task, expected_paths=tuple(dict.fromkeys((*task.expected_paths, *extra))))
        revised_plan = validate_plan_scope(replace(plan, tasks=tuple(
            revised_task if item.key == task.key else item for item in plan.tasks
        )), self.root)
        accepted_head = manager._git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip()
        stash_commit = manager.preserve_failed_attempt(
            workspace, f"MoliCoder run {run_id} task {task.key} needs broader approved scope",
        )
        if not stash_commit:
            raise ValueError("scope expansion had no failed attempt to preserve")
        current_payload = self.store.get_latest_plan_record(run_id)["payload"]
        model_overrides = current_payload.get("task_model_overrides", {})
        payload = revised_plan.to_dict() | {
            "runner_policy": self._runner_policy(strategy),
            "task_assignments": self._task_assignments(revised_plan, strategy, model_overrides),
            "task_model_overrides": model_overrides,
            "review_warnings": plan_review_warnings(revised_plan),
        }
        revision = self.store.save_plan(
            run_id, "scope_expansion_proposed", self.provider.name, payload,
            expected_latest_id=approved_plan_id,
        )
        self.store.update_run_task(
            task_id, "waiting_approval", attempts,
            reason="repair changed paths outside the approved task scope",
        )
        self.store.update_run_state(
            run_id, "awaiting_approval", worktree=str(workspace.path),
            final_commit=accepted_head, recovery_reason=error,
        )
        self._event(run_id, "plan_scope_expansion_proposed", {
            "previous_revision": approved_plan_id, "revision": revision,
            "digest": plan_digest(payload), "task_key": task.key,
            "additional_paths": extra, "accepted_checkpoint": accepted_head,
            "failed_attempt_stash": stash_commit,
        }, task_id)
        plan_path = self.root / ".molicoder" / "plans" / f"plan-{run_id}.json"
        try:
            plan_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="")
        except OSError as exc:
            self._event(run_id, "plan_export_failed", {"revision": revision, "reason": str(exc)[:500]})
        return OrchestrationResult(
            run_id, "awaiting_approval", completed, len(plan.tasks),
            "Repair needs approval for additional paths: " + ", ".join(extra),
            workspace.path, workspace.branch, workspace.base_commit, accepted_head,
        )

    def _run(self, request: str, *, mode: str = "HYBRID", approved_run_id: int | None = None) -> OrchestrationResult:
        strategy = self.normalize_strategy(mode)
        run_id = approved_run_id or self.store.create_run(
            self.project.id, request, strategy,
            provider=self.provider.name, model=self.provider.model, log_version=2,
        )
        self._active_run_id = run_id
        self.store.record_run_owner(run_id)
        if self.inference_gateway is not None:
            self.inference_gateway.run_id = run_id
            self.inference_gateway.task_id = None
        log_path = self.root / ".molicoder" / "logs" / f"run-{run_id}.jsonl"
        self._event(run_id, "run_started" if approved_run_id is None else "run_approved_execution_started", {
            "request": request, "strategy": strategy, "provider": self.provider.name,
            "model": self.provider.model or None, "isolation": "single-run-worktree",
            "log_path": str(log_path), "log_version": 2,
        })
        issue = clarification_needed(request)
        if issue:
            return self._finish_run(run_id, "needs_attention", 0, 0, error=issue)
        if self.route_decision is not None:
            self._event(run_id, "routing_decision", self.route_decision)
        if self.should_cancel():
            return self._finish_run(run_id, "cancelled", 0, 0, error="cancelled by user")
        self._event(run_id, "run_stage", {"stage": "preflight", "message": "Checking repository and provider"})
        if approved_run_id is None:
            self.store.update_run_state(run_id, "preflight")
        try:
            packet = Reconnaissance(self.root).build(request)
            self._event(run_id, "recon_completed", packet.to_dict())
            from .plan_scope import validate_plan_scope
            if approved_run_id is None:
                self._event(run_id, "run_stage", {"stage": "planning", "message": f"{self.provider.name.title()} is creating the plan"})
                self.store.update_run_state(run_id, "planning")
                plan = validate_plan_scope(self.planner.plan(packet), self.root)
                if len(plan.tasks) > 5:
                    raise ValueError("plan exceeds the five-task run limit")
                plan_payload = plan.to_dict() | {
                    "runner_policy": self._runner_policy(strategy),
                    "task_assignments": self._task_assignments(plan, strategy),
                    "review_warnings": plan_review_warnings(plan),
                }
                assignments = plan_payload["task_assignments"]
                plan_revision = self.store.save_plan(run_id, "generated", _planner_provider_name(self.planner), plan_payload)
                (self.root / ".molicoder" / "plans" / f"plan-{run_id}.json").write_text(
                    json.dumps(plan_payload, indent=2) + "\n", encoding="utf-8", newline="",
                )
                self._event(run_id, "plan_created", {
                    "provider": _planner_provider_name(self.planner), "tasks": len(plan.tasks),
                    "revision": plan_revision, "digest": plan_digest(plan_payload),
                })
                if self.require_plan_approval:
                    base_commit = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base)._git(
                        "rev-parse", "HEAD", cwd=self.root,
                    ).stdout.strip()
                    if not base_commit:
                        raise ValueError("Cannot bind the plan to a Git base commit")
                    self.store.update_run_state(run_id, "awaiting_approval", base_commit=base_commit)
                    self._event(run_id, "plan_awaiting_approval", {
                        "revision": plan_revision, "base_commit": base_commit,
                        "task_keys": [task.key for task in plan.tasks],
                    })
                    return OrchestrationResult(run_id, "awaiting_approval", 0, len(plan.tasks))
            else:
                approved_run, saved = require_approved_plan(self.store, run_id)
                approved_plan_id = int(saved["id"])
                approved_plan_digest = plan_digest(saved["payload"])
                if (str(approved_run["request"]) != request or str(approved_run["mode"]) != strategy
                        or str(approved_run["provider"]) != self.provider.name
                        or str(approved_run["model"]) != self.provider.model):
                    raise PermissionError("Approved Build route or request changed")
                policy = saved["payload"].get("runner_policy", {})
                if policy != self._runner_policy(strategy):
                    raise PermissionError("Approved runner policy changed")
                plan = validate_plan_scope(parse_plan(saved["payload"]), self.root)
                assignments = saved["payload"].get("task_assignments", {})
                if assignments != self._task_assignments(
                    plan, strategy, saved["payload"].get("task_model_overrides", {}),
                ):
                    raise PermissionError("Approved task assignments changed")
            manager = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base)
            if approved_run_id is not None and manager.base_commit != approved_run["base_commit"]:
                raise PermissionError("Project HEAD changed after plan approval")
            resume_approved_workspace = bool(approved_run_id is not None and approved_run.get("worktree"))
            if resume_approved_workspace:
                path = Path(str(approved_run["worktree"])).resolve()
                if path != (manager.base_dir / "workspace").resolve() or not path.is_dir():
                    raise ValueError("Approved repair workspace is missing or outside the managed run")
                branch = manager._git("branch", "--show-current", cwd=path).stdout.strip()
                if branch != f"molicoder/run-{run_id}":
                    raise ValueError("Approved repair workspace branch changed")
                head = manager._git("rev-parse", "HEAD", cwd=path).stdout.strip()
                if head != approved_run.get("final_commit") or self._changed_paths(manager, path):
                    raise ValueError("Approved repair checkpoint changed or contains unreviewed edits")
                workspace = TaskWorkspace("workspace", path, branch, str(approved_run["base_commit"]))
                existing_tasks = self.store.list_run_tasks(run_id)
                if {task.key for task in existing_tasks} != {task.key for task in plan.tasks}:
                    raise ValueError("Approved repair task graph differs from persisted tasks")
                for item in existing_tasks:
                    if item.status != "completed":
                        continue
                    completion = self.store.get_task_completion(item.id)
                    if completion is None or manager._git(
                        "merge-base", "--is-ancestor", completion.revision, head,
                        cwd=path, check=False,
                    ).returncode != 0:
                        raise ValueError("Accepted task checkpoint is missing from the repair workspace")
                self.store.reopen_run(run_id)
            else:
                workspace = manager.create_run()
            self.store.update_run_state(
                run_id, "running", base_commit=workspace.base_commit,
                worktree=str(workspace.path),
            )
            self._event(run_id, "workspace_reused" if resume_approved_workspace else "workspace_created", {
                "worktree": str(workspace.path), "branch": workspace.branch, "base_commit": workspace.base_commit,
            })
        except Exception as exc:
            return self._finish_run(run_id, "needs_attention", 0, 0, error=str(exc))

        task_ids: dict[str, int] = {}
        if resume_approved_workspace:
            task_ids = {task.key: task.id for task in existing_tasks}
        else:
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

        completed = sum(item.status == "completed" for item in existing_tasks) if resume_approved_workspace else 0
        for index, task in enumerate(plan.ordered_tasks(), start=1):
            if approved_run_id is not None:
                try:
                    _, current_plan = require_approved_plan(self.store, run_id)
                    if (int(current_plan["id"]) != approved_plan_id
                            or plan_digest(current_plan["payload"]) != approved_plan_digest):
                        raise PermissionError("The approved plan revision changed")
                except PermissionError as exc:
                    return self._finish_run(
                        run_id, "needs_attention", completed, len(plan.tasks),
                        error=f"Plan approval changed during execution: {exc}", workspace=workspace,
                    )
            if resume_approved_workspace and next(item for item in existing_tasks if item.key == task.key).status == "completed":
                continue
            self._active_provider = self.provider
            self.wait_if_paused()
            if self.should_cancel():
                self.cancel()
                return self._finish_run(run_id, "cancelled", completed, len(plan.tasks), error="cancelled by user", workspace=workspace)
            task_id = task_ids[task.key]
            self._active_task_id = task_id
            if self.inference_gateway is not None:
                self.inference_gateway.task_id = task_id
            prior_attempts = (
                next(item.attempts for item in existing_tasks if item.key == task.key)
                if (resume_approved_workspace and str(approved_run.get("recovery_reason") or "").startswith(
                    "Interrupted task output"
                )) else 0
            )
            if prior_attempts >= self.max_task_attempts:
                return self._finish_run(
                    run_id, "needs_attention", completed, len(plan.tasks),
                    error=f"Task {task.key} exhausted its approved attempt limit before recovery",
                    workspace=workspace,
                )
            self.store.update_run_task(task_id, "ready", prior_attempts, reason="dependencies satisfied")
            self.store.update_run_task(task_id, "running", prior_attempts)
            self._event(run_id, "task_started", asdict(task) | {
                "task_index": index, "task_total": len(plan.tasks), "worktree": str(workspace.path),
            }, task_id)
            validation_error = (
                str(approved_run.get("recovery_reason") or "") if resume_approved_workspace else ""
            )
            prompt = self._task_prompt(
                request, plan, task, packet, validation_error,
                run_id=run_id, task_id=task_id,
                base_commit=workspace.base_commit, remaining_attempts=self.max_task_attempts - prior_attempts,
            )
            attempts = prior_attempts
            changed: tuple[str, ...] = ()
            verified_no_change = False

            local_used = (
                assignments[task.key]["runner"] == "local" if approved_run_id is not None else
                strategy == "HYBRID" and self.local_executor is not None and self._local_eligible(task, workspace.path)
            )
            if strategy == "LOCAL_OFFLINE" and not (self.local_executor and self._local_eligible(task, workspace.path)):
                error = "Local offline mode only supports low-risk edits to one or two existing files."
                self.store.update_run_task(task_id, "blocked", 0, reason=error)
                self._event(run_id, "task_failed", {"key": task.key, "title": task.title, "reason": error, "worktree": str(workspace.path)}, task_id)
                return self._finish_run(run_id, "needs_attention", completed, len(plan.tasks), error=error, workspace=workspace)
            if local_used or strategy == "LOCAL_OFFLINE":
                self._event(run_id, "local_attempt_started", {"key": task.key, "reason": "bounded existing-file edit"}, task_id)
                local_outcome = self.local_executor(workspace.path, prompt)
                attempts = prior_attempts + 1
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
                    validation_error = self._task_validation(task, manager, workspace.path)
                    if self.should_cancel():
                        self.store.update_run_task(task_id, "cancelled", attempts, reason="cancelled during validation")
                        return self._finish_run(
                            run_id, "cancelled", completed, len(plan.tasks),
                            error="cancelled by user", workspace=workspace,
                        )
                    if validation_error.lstrip().startswith("validation passed"):
                        try:
                            changed = manager.commit(workspace, f"MoliCoder checkpoint {task.key}: {task.title}")
                            verified_no_change = not changed and bool(task.artifact_checks)
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
                if not changed and not verified_no_change and strategy == "HYBRID":
                    self._event(run_id, "provider_takeover", {"key": task.key, "provider": self.provider.name, "reason": validation_error or local_outcome.reason}, task_id)
                elif not changed and not verified_no_change:
                    error = validation_error or local_outcome.reason or "local validation failed"
                    self.store.update_run_task(task_id, "needs_attention", attempts)
                    return self._finish_run(run_id, "needs_attention", completed, len(plan.tasks), error=error, workspace=workspace)

            if not changed and not verified_no_change:
                wait_state = ""
                opaque_review_required = False
                assigned_model = assignments[task.key]["model"]
                attempt_provider = (
                    self._provider_for_model(assigned_model)
                    if assignments[task.key]["runner"] == "provider_cli" and assigned_model else self.provider
                )
                self._active_provider = attempt_provider
                failed_fingerprints: set[str] = set()
                validation_fingerprints: list[str] = []
                for frontier_attempt in range(prior_attempts + 1, self.max_task_attempts + 1):
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
                        self.cancel()
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
                    result = replace(
                        result, message=redact_text(result.message), error=redact_text(result.error),
                        events=tuple(_redact_event_value(event) for event in result.events),
                    )
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
                    validation_error = self._task_validation(task, manager, workspace.path)
                    if self.should_cancel():
                        self.store.update_run_task(task_id, "cancelled", attempts, reason="cancelled during validation")
                        return self._finish_run(
                            run_id, "cancelled", completed, len(plan.tasks),
                            error="cancelled by user", workspace=workspace,
                        )
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
                        if not validation_error.startswith("checkpoint failed:") and task.artifact_checks:
                            verified_no_change = True
                            self._event(run_id, "task_artifact_verified_without_git_change", {
                                "key": task.key, "paths": [check["path"] for check in task.artifact_checks],
                            }, task_id)
                            break
                        if not validation_error.startswith("checkpoint failed:"):
                            validation_error = "provider completed without making a repository change"
                    if validation_error.startswith("architectural review required:"):
                        break
                    if opaque_attempt:
                        validation_error = "Opaque agent output requires review before retry: " + validation_error
                        opaque_review_required = True
                        break
                    validation_fingerprints.append(failure_fingerprint(validation_error))
                    progress = classify_progress(validation_fingerprints)
                    if progress in {"stuck", "oscillating"}:
                        self._event(run_id, "repair_stopped_no_progress", {
                            "key": task.key, "attempt": frontier_attempt, "pattern": progress,
                            "diagnostic_fingerprint": validation_fingerprints[-1],
                        }, task_id)
                        validation_error = f"repair {progress} after repeated validation failure: " + validation_error
                        break
                    if frontier_attempt < self.max_task_attempts:
                        self._event(run_id, "repair_started", {"key": task.key, "reason": validation_error, "attempt": frontier_attempt + 1}, task_id)

            if not changed and not verified_no_change:
                error = validation_error or "provider did not complete the task"
                recovery = classify_task_failure("failed", error, validation_summary=validation_error)
                self._event(run_id, "task_recovery_decision", {
                    "key": task.key, "category": recovery.category,
                    "action": recovery.action, "retry_allowed": recovery.retry_allowed,
                    "fingerprint": recovery.fingerprint,
                }, task_id)
                needs_review = opaque_review_required or error.startswith("architectural review required:")
                if (approved_run_id is not None and attempts > 1 and not needs_review
                        and error.startswith("provider changed paths outside the approved task scope:")):
                    try:
                        proposal = self._propose_scope_revision(
                            run_id, plan, task, task_id, attempts, completed,
                            manager, workspace, approved_plan_id, strategy, error,
                        )
                    except (OSError, ValueError) as exc:
                        error = f"scope revision proposal failed: {exc}; original failure: {error}"
                    else:
                        if proposal is not None:
                            return proposal
                if opaque_review_required:
                    self._event(run_id, "opaque_attempt_review_required", {
                        "key": task.key, "attempt": attempts, "provider": attempt_provider.name,
                        "reason": error[:500], "worktree": str(workspace.path),
                    }, task_id)
                final_state = "review_required" if needs_review else wait_state or "needs_attention"
                self.store.update_run_task(task_id, final_state, attempts, reason=redact_text(error))
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
                validation_summary=validation_error,
                output_artifacts=changed or tuple(check["path"] for check in task.artifact_checks),
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
                "verified_no_change": verified_no_change,
            }, task_id)

        self._event(run_id, "run_stage", {"stage": "validating", "message": "Running final project validation"})
        self.store.update_run_state(run_id, "validating")
        final_validation = self.validate(workspace.path)
        if self.should_cancel():
            return self._finish_run(
                run_id, "cancelled", completed, len(plan.tasks),
                error="cancelled by user", workspace=workspace,
            )
        if final_validation.lstrip().startswith("validation passed"):
            failures = tuple(
                failure for task in plan.tasks
                for failure in verify_artifact_checks(workspace.path, task.artifact_checks)
            )
            if failures:
                final_validation = "artifact validation failed: " + "; ".join(failures)
        if not final_validation.lstrip().startswith("validation passed"):
            return self._finish_run(
                run_id, "needs_attention", completed, len(plan.tasks),
                error=final_validation, workspace=workspace,
            )
        if approved_run_id is not None:
            try:
                _, current_plan = require_approved_plan(self.store, run_id)
                if (int(current_plan["id"]) != approved_plan_id
                        or plan_digest(current_plan["payload"]) != approved_plan_digest):
                    raise PermissionError("The approved plan revision changed")
            except PermissionError as exc:
                return self._finish_run(
                    run_id, "needs_attention", completed, len(plan.tasks),
                    error=f"Plan approval changed before apply: {exc}", workspace=workspace,
                )
        if self.should_cancel():
            return self._finish_run(
                run_id, "cancelled", completed, len(plan.tasks),
                error="cancelled by user", workspace=workspace,
            )
        final_commit = manager._git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip()
        if self.auto_apply and not any(
            check["kind"] == "directory_empty" for task in plan.tasks for check in task.artifact_checks
        ):
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
        else:
            return self._finish_run(
                run_id, "ready_to_apply", completed, len(plan.tasks),
                error="Validated result is ready for review and apply",
                workspace=workspace, final_commit=final_commit,
            )
        return self._finish_run(
            run_id, "completed", completed, len(plan.tasks), workspace=workspace, final_commit=final_commit,
        )

    def _propose_interrupted_recovery(self, run_id: int, run: dict) -> OrchestrationResult:
        """Preserve unverified edits and require fresh approval before continuing."""
        from .plan_scope import validate_plan_scope
        from .recovery import observe_interrupted_run

        tasks = self.store.list_run_tasks(run_id)
        completed = sum(task.status == "completed" for task in tasks)
        path = Path(str(run.get("worktree") or "")).resolve()

        def blocked(reason: str) -> OrchestrationResult:
            self._event(run_id, "run_resume_blocked_interrupted", {"reason": reason})
            return OrchestrationResult(run_id, "needs_attention", completed, len(tasks), reason, path)

        if not self.require_plan_approval:
            return blocked("Interrupted Build requires plan approval before continuation")
        if str(run["provider"]) != self.provider.name or str(run["model"]) != self.provider.model:
            return blocked("Interrupted Build provider/model changed; restore the saved route before recovery")
        latest = self.store.get_latest_plan_record(run_id)
        if latest is None:
            return blocked("Interrupted Build plan is missing")
        try:
            plan = validate_plan_scope(parse_plan(latest["payload"]), self.root)
            strategy = self.normalize_strategy(str(run["mode"]))
            if (latest["payload"].get("runner_policy") != self._runner_policy(strategy)
                    or latest["payload"].get("task_assignments") != self._task_assignments(
                        plan, strategy, latest["payload"].get("task_model_overrides", {}),
                    )):
                return blocked("Interrupted Build runner assignments differ from the saved plan")
            if {task.key for task in tasks} != {task.key for task in plan.tasks}:
                return blocked("Interrupted Build task graph differs from the saved plan")
            manager = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base)
            if manager.base_commit != run.get("base_commit"):
                return blocked("Project HEAD changed since the interrupted Build; review before retry")
            if path != (manager.base_dir / "workspace").resolve() or not path.is_dir():
                return blocked("Interrupted Build workspace is missing or outside the managed run")
            observed = observe_interrupted_run(self.store, run_id)
            if observed.get("workspace_state") not in {"dirty", "clean_unverified", "clean"}:
                return blocked(f"Interrupted Build worktree failed identity check: {observed.get('workspace_state')}")
            accepted_head = str(run.get("base_commit") or "")
            for task in tasks:
                completion = self.store.get_task_completion(task.id)
                if task.status == "completed" and completion is None:
                    return blocked(f"Completed task {task.key} has no durable verification checkpoint")
                if task.status != "completed" and completion is not None:
                    return blocked(f"Task {task.key} has an unreviewed completion record")
                if completion is not None:
                    revision = completion.revision
                    if manager._git("merge-base", "--is-ancestor", accepted_head, revision,
                                    cwd=path, check=False).returncode != 0:
                        return blocked("Accepted task checkpoints are not a linear verified history")
                    accepted_head = revision
            if observed.get("checkpoint") != accepted_head:
                return blocked("Interrupted Build contains an unverified commit; review its checkpoint before retry")
            if not any(task.status != "completed" for task in tasks):
                return blocked("All tasks have completion records; review final validation before applying")
            workspace = TaskWorkspace("workspace", path, f"molicoder/run-{run_id}", str(run["base_commit"]))
            stash = manager.preserve_failed_attempt(
                workspace, f"MoliCoder interrupted run {run_id} unverified output",
            )
            if manager._git("rev-parse", "HEAD", cwd=path).stdout.strip() != accepted_head or self._changed_paths(manager, path):
                return blocked("Interrupted Build workspace changed while preserving unverified output")
            revision = self.store.save_plan(
                run_id, "interrupted_recovery_proposed", self.provider.name, latest["payload"],
                expected_latest_id=int(latest["id"]),
            )
            self.store.update_run_state(
                run_id, "awaiting_approval", final_commit=accepted_head,
                recovery_reason="Interrupted task output was preserved for review; approve this revision to retry unfinished tasks",
            )
            self._event(run_id, "plan_recovery_proposed", {
                "previous_revision": int(latest["id"]), "revision": revision,
                "digest": plan_digest(latest["payload"]), "accepted_checkpoint": accepted_head,
                "accepted_tasks": completed, "unverified_stash": stash,
            })
            return OrchestrationResult(
                run_id, "awaiting_approval", completed, len(tasks),
                "Review the recovered checkpoint and approve the new plan revision",
                path, workspace.branch, workspace.base_commit, accepted_head,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return blocked(f"Interrupted Build recovery requires review: {exc}")

    def resume(self, run_id: int) -> OrchestrationResult:
        """Resume a preserved run worktree with a fresh provider context."""
        run = self.store.get_run(run_id)
        if run is None or int(run["project_id"]) != self.project.id:
            raise ValueError("run does not belong to the selected project")
        if run["status"] not in {"needs_attention", "ready_to_apply", "cancelled", "failed"}:
            raise ValueError(f"run cannot be resumed from status {run['status']}")
        recovery_observation = next((
            event for event in reversed(self.store.list_events(run_id))
            if event.kind == "run_recovery_observed"
        ), None)
        if recovery_observation is not None:
            return self._propose_interrupted_recovery(run_id, run)
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
        target_plan = next(task for task in plan.tasks if task.key == target.key)
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
            if self.should_cancel():
                self.store.update_run_task(target.id, "cancelled", attempt, reason="cancelled during validation")
                return self._finish_run(
                    run_id, "cancelled", 0, len(plan.tasks),
                    error="cancelled by user", workspace=workspace,
                )
            if validation_error.lstrip().startswith("validation passed"):
                failures = tuple(
                    failure for task in plan.tasks
                    for failure in verify_artifact_checks(path, task.artifact_checks)
                )
                if failures:
                    validation_error = "artifact validation failed: " + "; ".join(failures)
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
                if not changed and not target.changed_paths and not target_plan.artifact_checks:
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
                    output_artifacts=changed or target.changed_paths or tuple(
                        check["path"] for check in target_plan.artifact_checks
                    ),
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
                if (self.auto_apply and allowed and not any(
                    check["kind"] == "directory_empty"
                    for task in plan.tasks for check in task.artifact_checks
                )):
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
