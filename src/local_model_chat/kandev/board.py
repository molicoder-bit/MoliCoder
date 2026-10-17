"""Approval-gated projection of a Build DAG onto Kandev's pinned task API."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from ..domain import Project
from ..molicoder.orchestrator import OrchestrationResult
from ..molicoder.plan_approval import plan_digest, require_approved_plan
from ..molicoder.plan_scope import validate_plan_scope
from ..molicoder.planner.schema import parse_plan
from ..molicoder.artifact_checks import verify_artifact_checks
from ..molicoder.worktree import IntegrationConflict, TaskWorkspace, WorktreeManager
from ..molicoder.validate import assess_architectural_invariants
from ..molicoder.recon import Reconnaissance
from ..molicoder.providers import ProviderUnavailable, redact_text
from ..storage import ChatStore
from .adapter import KandevTaskAdapter
from .contracts import KandevTaskRef, KandevTaskSnapshot
from .worktree import verify_kandev_worktree


class KandevBoardOrchestrator:
    """Create each approved task with real Kandev blocker and profile fields.

    Remote completion remains a verification request. This bridge does not
    accept or integrate remote Git output on its own.
    """

    def __init__(
        self, *, store: ChatStore, project: Project, adapter: KandevTaskAdapter,
        planner, task_profiles: dict[str, str],
        authorize_launch: Callable[[KandevTaskRef], None],
        profile_models: dict[str, str] | None = None,
        validate_task: Callable[[Path, object], str] | None = None,
        validate_integration: Callable[[Path], str] | None = None,
        repair_task: Callable[[Path, object, str], None] | None = None,
        resolve_conflict: Callable[[Path, tuple[str, ...]], None] | None = None,
        worktree_base: str | Path | None = None,
        max_repair_attempts: int = 2,
    ) -> None:
        self.store = store
        self.project = project
        self.root = Path(project.path).resolve()
        self.adapter = adapter
        self.planner = planner
        self.task_profiles = dict(task_profiles)
        self.profile_models = dict(profile_models or {})
        self.authorize_launch = authorize_launch
        self.validate_task = validate_task
        self.validate_integration = validate_integration
        self.repair_task = repair_task
        self.resolve_conflict = resolve_conflict
        self.worktree_base = worktree_base
        self.max_repair_attempts = max(0, min(int(max_repair_attempts), 2))
        if not self.root.is_dir():
            raise ValueError("project folder does not exist")

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _clean_head(self) -> str:
        head = self._git("rev-parse", "HEAD")
        branch = self._git("branch", "--show-current")
        dirty = self._git("status", "--porcelain", "--untracked-files=all")
        if (head.returncode or branch.returncode or dirty.returncode
                or not branch.stdout.strip() or dirty.stdout.strip()):
            raise ValueError("Kandev Build requires a clean Git branch with a base commit")
        return head.stdout.strip()

    def _policy(self) -> dict:
        config = getattr(self.adapter.api, "config", None)
        allowed = {str(getattr(config, "agent_profile_id", "")),
                   *(str(value) for value in getattr(config, "additional_agent_profile_ids", ()))}
        if not allowed or "" in allowed or any(value not in allowed for value in self.task_profiles.values()):
            raise ValueError("Kandev task profile is outside the configured allowlist")
        return {
            "backend": "kandev-board", "endpoint": self.adapter.connection.endpoint,
            "version": self.adapter.connection.version,
            "repository_id": str(getattr(config, "repository_id", "")),
            "task_profiles": self.task_profiles,
            "profile_models": self.profile_models,
        }

    def run(self, request: str, *, mode: str = "HYBRID") -> OrchestrationResult:
        if not request.strip():
            raise ValueError("Build request is empty")
        if self.adapter.probe().health_state != "ready":
            raise RuntimeError("Kandev task API is not ready")
        base = self._clean_head()
        planned = self.planner.plan(Reconnaissance(self.root).build(request))
        if not 1 <= len(planned.tasks) <= 30:
            raise ValueError("Kandev board plans require between 1 and 30 tasks")
        planned.ordered_tasks()
        plan = validate_plan_scope(planned, self.root)
        if set(self.task_profiles) != {task.key for task in plan.tasks}:
            raise ValueError("Every Kandev task needs one configured agent profile")
        policy = self._policy()
        run_id = self.store.create_run(
            self.project.id, request, mode, provider="kandev", base_commit=base,
        )
        from ..orchestration.coordinator import RequestCoordinator
        RequestCoordinator.register_build(self.store, self.project, run_id, request, mode, provider='kandev-board')
        payload = plan.to_dict() | {
            "runner_policy": policy,
            "task_assignments": {
                task.key: {
                    "runner": "kandev", "agent_profile_id": self.task_profiles[task.key],
                    "model": self.profile_models.get(self.task_profiles[task.key], "unknown"),
                }
                for task in plan.tasks
            },
        }
        revision = self.store.save_plan(run_id, "generated", "kandev-board", payload)
        task_ids = {
            task.key: self.store.create_run_task(
                run_id, task.key, task.title, task.objective, task.risk,
                acceptance_criteria=task.acceptance_criteria,
                verification_checks=task.verification_checks,
            ) for task in plan.tasks
        }
        for task in plan.tasks:
            for parent in task.depends_on:
                self.store.add_task_dependency(task_ids[task.key], task_ids[parent])
        self.store.update_run_state(run_id, "awaiting_approval")
        self.store.add_event(run_id, "kandev_board_plan_awaiting_approval", {
            "revision": revision, "base_commit": base,
            "task_keys": [task.key for task in plan.tasks],
        })
        return OrchestrationResult(run_id, "awaiting_approval", 0, len(plan.tasks))

    def _check_snapshot(
        self, snapshot: KandevTaskSnapshot, ref: KandevTaskRef,
        parents: tuple[str, ...], profile: str,
    ) -> None:
        if (snapshot.ref.external_id != ref.external_id or not snapshot.ref.remote_task_id
                or snapshot.state == "reconciliation_required"):
            raise ValueError("Kandev task identity needs reconciliation")
        if set(snapshot.depends_on_task_ids) != set(parents):
            raise ValueError("Kandev dependency graph differs from the approved plan")
        if snapshot.agent_profile_id != profile:
            raise ValueError("Kandev agent profile differs from the approved assignment")
        expected_model = self.profile_models.get(profile)
        if expected_model and snapshot.model and snapshot.model != expected_model:
            raise ValueError("Kandev observed model differs from the approved profile model")
        if expected_model and snapshot.state in {"running", "verifying"} and not snapshot.model:
            raise ValueError("Kandev running task has no verifiable model assignment")
        if parents and not snapshot.start_when_unblocked:
            raise ValueError("Kandev dependent task lacks automatic activation intent")

    def execute_approved(self, run_id: int) -> OrchestrationResult:
        run, saved = require_approved_plan(self.store, run_id)
        if (int(run["project_id"]) != self.project.id or run["status"] != "awaiting_approval"
                or run["provider"] != "kandev"):
            raise PermissionError("Kandev board is not awaiting approval for this project")
        if self._clean_head() != run["base_commit"]:
            raise PermissionError("Project HEAD changed after Kandev board approval")
        plan = validate_plan_scope(parse_plan(saved["payload"], max_tasks=30), self.root)
        if saved["payload"].get("runner_policy") != self._policy():
            raise PermissionError("Approved Kandev board policy changed")
        if saved["payload"].get("task_assignments") != {
            task.key: {
                "runner": "kandev", "agent_profile_id": self.task_profiles[task.key],
                "model": self.profile_models.get(self.task_profiles[task.key], "unknown"),
            }
            for task in plan.tasks
        }:
            raise PermissionError("Approved Kandev task assignments changed")
        tasks = {task.key: task for task in self.store.list_run_tasks(run_id)}
        if set(tasks) != {task.key for task in plan.tasks}:
            raise ValueError("Persisted Kandev task graph differs from the approved plan")
        approved_id, approved_digest = int(saved["id"]), plan_digest(saved["payload"])
        remote_ids: dict[str, str] = {}
        self.store.update_run_state(run_id, "scheduling")
        for task in plan.ordered_tasks():
            try:
                _, current = require_approved_plan(self.store, run_id)
                if int(current["id"]) != approved_id or plan_digest(current["payload"]) != approved_digest:
                    raise PermissionError("Kandev board approval changed during creation")
            except PermissionError:
                self.store.update_run_state(
                    run_id, "needs_attention",
                    recovery_reason="Kandev board approval changed during creation",
                )
                return OrchestrationResult(
                    run_id, "needs_attention", 0, len(plan.tasks),
                    "Kandev board approval changed during creation",
                )
            task_id = tasks[task.key].id
            ref = KandevTaskRef(
                run_id, task_id,
                f"molicoder:project:{self.project.id}:run:{run_id}:task:{task_id}",
            )
            parents = tuple(remote_ids[key] for key in task.depends_on)
            profile = self.task_profiles[task.key]
            self.store.add_event(run_id, "kandev_board_owner_selected", {
                "task_key": task.key, "external_id": ref.external_id,
                "blocked_by": parents, "agent_profile_id": profile,
            }, task_id)
            try:
                self.authorize_launch(ref)
                snapshot = self.adapter.create_task(
                    ref, task.objective, blocked_by=parents, agent_profile_id=profile,
                )
                self._check_snapshot(snapshot, ref, parents, profile)
                observed = self.adapter.reconcile(snapshot.ref)
                self._check_snapshot(observed, ref, parents, profile)
            except Exception as exc:
                reason = redact_text(str(exc))
                self.store.update_run_task(task_id, "review_required", 0, reason=reason)
                self.store.update_run_state(
                    run_id, "needs_attention",
                    recovery_reason="Kandev task creation or mapping requires reconciliation",
                )
                self.store.add_event(run_id, "kandev_board_reconciliation_required", {
                    "task_key": task.key, "external_id": ref.external_id,
                    "reason": reason,
                }, task_id)
                return OrchestrationResult(
                    run_id, "needs_attention", 0, len(plan.tasks),
                    "Kandev task creation or mapping requires reconciliation",
                )
            remote_ids[task.key] = snapshot.ref.remote_task_id
            self.store.update_run_task(
                task_id, "running" if observed.state == "running" else "ready", 0,
                reason="Kandev owns execution; local checkpoint verification is pending",
            )
            self.store.add_event(run_id, "kandev_board_task_created", {
                "task_key": task.key, "external_id": ref.external_id,
                "remote_task_id": snapshot.ref.remote_task_id,
                "blocked_by": parents, "agent_profile_id": profile,
                "start_when_unblocked": observed.start_when_unblocked,
            }, task_id)
        self.store.update_run_state(run_id, "running")
        return OrchestrationResult(run_id, "running", 0, len(plan.tasks))

    def reconcile_all(self, run_id: int) -> OrchestrationResult:
        """Refresh remote board state without treating remote completion as verified code."""
        run = self.store.get_run(run_id)
        if run is None or int(run["project_id"]) != self.project.id or run["provider"] != "kandev":
            raise ValueError("run is not a Kandev board for this project")
        plan_record = self.store.get_latest_plan_record(run_id)
        if plan_record is None or plan_record["payload"].get("runner_policy") != self._policy():
            raise ValueError("Kandev board plan or runner policy differs")
        rows = self.store.list_run_tasks(run_id)
        if run["status"] in {"ready_to_apply", "completed"} and all(
            self.store.get_task_completion(row.id) is not None for row in rows
        ):
            return OrchestrationResult(run_id, str(run["status"]), len(rows), len(rows))
        created = {}
        owners = {}
        for event in self.store.list_events(run_id):
            if event.kind in {"kandev_board_task_created", "kandev_board_owner_selected"}:
                payload = json.loads(event.payload_json)
                if event.kind == "kandev_board_task_created":
                    created[payload["task_key"]] = payload
                else:
                    owners[payload["task_key"]] = payload
        if not owners and not created:
            return OrchestrationResult(run_id, str(run["status"]), 0, len(self.store.list_run_tasks(run_id)))
        attention = False
        for task in self.store.list_run_tasks(run_id):
            mapping = created.get(task.key)
            if mapping is None and task.key in owners:
                owner = owners[task.key]
                pending_ref = KandevTaskRef(run_id, task.id, owner["external_id"])
                try:
                    recovered = self.adapter.reconcile(pending_ref)
                    self._check_snapshot(
                        recovered, pending_ref, tuple(owner["blocked_by"]), owner["agent_profile_id"],
                    )
                    mapping = owner | {"remote_task_id": recovered.ref.remote_task_id}
                    self.store.add_event(run_id, "kandev_board_task_created", mapping, task.id)
                    created[task.key] = mapping
                except Exception as exc:
                    attention = True
                    self.store.update_run_task(
                        task.id, "review_required", task.attempts, reason=redact_text(str(exc)),
                    )
                    continue
            if mapping is None:
                attention = True
                continue
            ref = KandevTaskRef(
                run_id, task.id, mapping["external_id"], mapping["remote_task_id"],
            )
            try:
                snapshot = self.adapter.reconcile(ref)
                self._check_snapshot(
                    snapshot, ref, tuple(mapping["blocked_by"]), mapping["agent_profile_id"],
                )
            except Exception as exc:
                attention = True
                self.store.update_run_task(
                    task.id, "review_required", task.attempts, reason=redact_text(str(exc)),
                )
                continue
            local_state = {
                "ready": "ready", "running": "running", "verifying": "verifying",
                "failed": "failed", "cancelled": "cancelled",
            }.get(snapshot.state, "review_required")
            if local_state not in {"ready", "running"}:
                attention = True
            self.store.update_run_task(
                task.id, local_state, task.attempts,
                reason="Remote completion awaits local Git and project validation"
                if local_state == "verifying" else snapshot.detail,
            )
            self.store.add_event(run_id, "kandev_board_task_observed", {
                "task_key": task.key, "remote_task_id": ref.remote_task_id,
                "state": snapshot.state, "agent_profile_id": snapshot.agent_profile_id,
                "model": snapshot.model,
            }, task.id)
        status = "needs_attention" if attention else "running"
        self.store.update_run_state(
            run_id, status,
            recovery_reason="Kandev board has remote results requiring local verification" if attention else "",
        )
        return OrchestrationResult(run_id, status, 0, len(self.store.list_run_tasks(run_id)))

    def verify_and_integrate(self, run_id: int) -> OrchestrationResult:
        """Accept remote checkpoints only after scoped task and project checks."""
        if self.validate_task is None or self.validate_integration is None:
            raise ValueError("Kandev board verification callbacks are not configured")
        run, saved = require_approved_plan(self.store, run_id)
        if int(run["project_id"]) != self.project.id or run["provider"] != "kandev":
            raise ValueError("run is not a Kandev board for this project")
        if run["status"] not in {"running", "needs_attention"}:
            raise ValueError("Kandev board is not ready for checkpoint verification")
        if saved["payload"].get("runner_policy") != self._policy():
            raise PermissionError("Kandev board runner policy changed")
        plan = validate_plan_scope(parse_plan(saved["payload"], max_tasks=30), self.root)
        if self._clean_head() != run["base_commit"]:
            raise PermissionError("Project changed before Kandev integration")
        task_rows = {row.key: row for row in self.store.list_run_tasks(run_id)}
        mappings = {}
        for event in self.store.list_events(run_id):
            if event.kind == "kandev_board_task_created":
                payload = json.loads(event.payload_json)
                mappings[payload["task_key"]] = payload
        manager = WorktreeManager(self.root, run_id=run_id, base_dir=self.worktree_base)
        if manager.base_commit != run["base_commit"]:
            raise PermissionError("Kandev integration base differs from the approved project")
        approved_id, approved_digest = int(saved["id"]), plan_digest(saved["payload"])
        accepted: dict[str, TaskWorkspace] = {}

        def needs_attention(reason: str, completed: int) -> OrchestrationResult:
            safe_reason = redact_text(reason)
            self.store.update_run_state(run_id, "needs_attention", recovery_reason=safe_reason)
            self.store.add_event(run_id, "kandev_board_verification_stopped", {
                "reason": safe_reason, "completed_tasks": completed,
            })
            return OrchestrationResult(run_id, "needs_attention", completed, len(plan.tasks), safe_reason)

        for task in plan.ordered_tasks():
            try:
                _, current = require_approved_plan(self.store, run_id)
                approval_current = int(current["id"]) == approved_id and plan_digest(current["payload"]) == approved_digest
            except PermissionError:
                approval_current = False
            if not approval_current:
                return needs_attention("Kandev board approval changed during verification", len(accepted))
            mapping = mappings.get(task.key)
            row = task_rows.get(task.key)
            if mapping is None or row is None:
                return needs_attention(f"Kandev task {task.key} has no verified remote identity", len(accepted))
            ref = KandevTaskRef(run_id, row.id, mapping["external_id"], mapping["remote_task_id"])
            try:
                snapshot = self.adapter.reconcile(ref)
                self._check_snapshot(
                    snapshot, ref, tuple(mapping["blocked_by"]), mapping["agent_profile_id"],
                )
                if snapshot.state != "verifying":
                    return needs_attention(f"Kandev task {task.key} is not remotely complete", len(accepted))
                check = verify_kandev_worktree(
                    self.root, snapshot, expected_repository_id=self._policy()["repository_id"],
                )
                if not check.valid or check.base_commit != run["base_commit"]:
                    return needs_attention(
                        f"Kandev task {task.key} worktree requires review: {check.reason or 'base differs'}",
                        len(accepted),
                    )
                remote_workspace = TaskWorkspace(task.key, Path(check.path), check.branch, check.base_commit)
                workspace = remote_workspace
                changed = self._changed_paths(manager, workspace, check.base_commit)
                if not changed or not self._within_scope(task, changed):
                    return needs_attention(f"Kandev task {task.key} changed paths outside approved scope", len(accepted))
                summary = self._validate_task_output(task, workspace.path, changed)
                rechecked = verify_kandev_worktree(
                    self.root, snapshot, expected_repository_id=self._policy()["repository_id"],
                )
                if not rechecked.valid or rechecked.checkpoint != check.checkpoint:
                    return needs_attention(
                        f"Kandev task {task.key} changed during validation", len(accepted),
                    )
                attempts = 0
                if not summary.lstrip().startswith("validation passed"):
                    if self.repair_task is None or self.max_repair_attempts == 0:
                        return needs_attention(f"Kandev task {task.key} failed validation: {summary}", len(accepted))
                    workspace = manager.create_task(f"repair-{task.key}")
                    manager.merge_into(workspace, remote_workspace)
                    for attempts in range(1, self.max_repair_attempts + 1):
                        try:
                            self.repair_task(workspace.path, task, summary)
                        except ProviderUnavailable as exc:
                            dirty = manager._git(
                                "status", "--porcelain", "--untracked-files=all",
                                cwd=workspace.path,
                            ).stdout.strip()
                            if dirty:
                                return needs_attention(
                                    f"Kandev task {task.key} has ambiguous repair output after provider loss",
                                    len(accepted),
                                )
                            self.store.add_event(run_id, "kandev_board_repair_route_unavailable", {
                                "task_key": task.key, "attempt": attempts,
                                "reason": redact_text(str(exc)),
                            }, row.id)
                            if attempts == self.max_repair_attempts:
                                return needs_attention(
                                    f"Kandev task {task.key} exhausted repair routes", len(accepted),
                                )
                            continue
                        changed = self._changed_paths(manager, workspace, check.base_commit, include_dirty=True)
                        summary = self._validate_task_output(task, workspace.path, changed)
                        self.store.add_event(run_id, "kandev_board_repair_checked", {
                            "task_key": task.key, "attempt": attempts,
                            "passed": summary.lstrip().startswith("validation passed"),
                        }, row.id)
                        if summary.lstrip().startswith("validation passed"):
                            manager.commit(workspace, f"MoliCoder Kandev repair {task.key}")
                            changed = self._changed_paths(manager, workspace, check.base_commit)
                            break
                        if attempts < self.max_repair_attempts:
                            manager.preserve_failed_attempt(
                                workspace, f"Kandev task {task.key} repair attempt {attempts}",
                            )
                    if not summary.lstrip().startswith("validation passed"):
                        return needs_attention(f"Kandev task {task.key} exhausted repair attempts", len(accepted))
                checkpoint = manager._git("rev-parse", "HEAD", cwd=workspace.path).stdout.strip()
                if manager._git(
                    "status", "--porcelain", "--untracked-files=all", cwd=workspace.path,
                ).stdout.strip():
                    return needs_attention(
                        f"Kandev task {task.key} has unverified changes after repair", len(accepted),
                    )
                self.store.record_task_completion(
                    run_id, row.id, revision=checkpoint,
                    checks=task.verification_checks, validation_summary=summary,
                    output_artifacts=changed, unmet_criteria=(), human_review_status="not_required",
                )
                self.store.update_task_workspace(
                    row.id, worktree=str(workspace.path), branch=workspace.branch,
                    base_commit=check.base_commit, changed_paths=changed,
                )
                self.store.update_run_task(row.id, "completed", attempts, reason="remote checkpoint passed local validation")
                self.store.add_event(run_id, "kandev_board_checkpoint_accepted", {
                    "task_key": task.key, "checkpoint": checkpoint,
                    "changed_paths": changed, "repair_attempts": attempts,
                }, row.id)
                accepted[task.key] = workspace
            except Exception as exc:
                return needs_attention(f"Kandev task {task.key} verification failed: {exc}", len(accepted))

        try:
            try:
                _, current = require_approved_plan(self.store, run_id)
                approval_current = int(current["id"]) == approved_id and plan_digest(current["payload"]) == approved_digest
            except PermissionError:
                approval_current = False
            if not approval_current:
                return needs_attention("Kandev board approval changed before integration", len(accepted))
            integration = manager.create_integration()
            for task in plan.ordered_tasks():
                try:
                    manager.merge_into(
                        integration, accepted[task.key],
                        preserve_conflict=self.resolve_conflict is not None,
                    )
                except IntegrationConflict as conflict:
                    self.store.add_event(run_id, "kandev_board_integration_conflict", {
                        "task_key": task.key, "paths": conflict.paths,
                    })
                    if self.resolve_conflict is None:
                        raise
                    self.resolve_conflict(integration.path, conflict.paths)
                    manager.complete_conflict_merge(integration, conflict.paths)
            summary = self.validate_integration(integration.path)
            if not summary.lstrip().startswith("validation passed"):
                return needs_attention("Kandev board project validation failed: " + summary, len(accepted))
            if self._clean_head() != run["base_commit"]:
                return needs_attention("Project changed during Kandev integration", len(accepted))
            final = manager._git("rev-parse", "HEAD", cwd=integration.path).stdout.strip()
            self.store.update_run_state(
                run_id, "ready_to_apply", worktree=str(integration.path),
                base_commit=str(run["base_commit"]), final_commit=final,
            )
            self.store.add_event(run_id, "kandev_board_integration_verified", {
                "checkpoint": final, "task_count": len(accepted), "summary": summary,
            })
            return OrchestrationResult(
                run_id, "ready_to_apply", len(accepted), len(plan.tasks), "",
                integration.path, integration.branch, str(run["base_commit"]), final,
            )
        except Exception as exc:
            return needs_attention(f"Kandev board integration failed: {exc}", len(accepted))

    @staticmethod
    def _within_scope(task, changed: tuple[str, ...]) -> bool:
        expected = tuple(path.rstrip("/") for path in task.expected_paths)
        return all(
            any(path == scope or path.startswith(scope + "/") for scope in expected)
            for path in changed
        )

    @staticmethod
    def _changed_paths(
        manager: WorktreeManager, workspace: TaskWorkspace, base: str,
        *, include_dirty: bool = False,
    ) -> tuple[str, ...]:
        committed = manager._git(
            "diff", "--name-only", base, "HEAD", "--", cwd=workspace.path,
        ).stdout.splitlines()
        dirty = manager._git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace.path,
        ).stdout.splitlines() if include_dirty else ()
        paths = {line.strip().replace("\\", "/") for line in committed if line.strip()}
        paths.update(line[3:].strip().replace("\\", "/") for line in dirty if len(line) >= 4)
        return tuple(sorted(paths))

    def _validate_task_output(self, task, worktree: Path, changed: tuple[str, ...]) -> str:
        if not self._within_scope(task, changed):
            return "task changed paths outside approved scope"
        resolved_root = worktree.resolve()
        if any(
            (resolved_root / path).is_symlink()
            or not (resolved_root / path).resolve().is_relative_to(resolved_root)
            for path in changed
        ):
            return "task output contains an unsafe symbolic link or path escape"
        assessment = assess_architectural_invariants(self.store, self.project.id, changed)
        if assessment.conflicts or assessment.review_required:
            return "task requires architectural review"
        summary = self.validate_task(worktree, task)
        if summary.lstrip().startswith("validation passed"):
            failures = verify_artifact_checks(worktree, task.artifact_checks)
            if failures:
                return "artifact validation failed: " + "; ".join(failures)
        return summary
