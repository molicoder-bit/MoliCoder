from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable

from ..domain import Project
from ..molicoder.orchestrator import OrchestrationResult
from ..molicoder.plan_approval import require_approved_plan
from ..molicoder.planner.schema import parse_plan
from ..storage import ChatStore
from ..molicoder.validate import assess_architectural_invariants
from .adapter import KandevTaskAdapter
from .contracts import KandevTaskRef, KandevTaskSnapshot
from .rest import KandevTransportError
from .worktree import verify_kandev_worktree


class KandevRunOrchestrator:
    """Submit one Build request to Kandev and retain its remote ownership."""

    def __init__(
        self, *, store: ChatStore, project: Project, adapter: KandevTaskAdapter,
        on_event: Callable[[str, dict], None] | None = None,
        validate: Callable[[Path], str] | None = None,
        authorize_launch: Callable[[KandevTaskRef], None] | None = None,
        require_plan_approval: bool = False,
    ) -> None:
        self.store = store
        self.project = project
        self.root = Path(project.path).resolve()
        self.adapter = adapter
        self.on_event = on_event
        self.validate = validate
        self.authorize_launch = authorize_launch
        self.require_plan_approval = require_plan_approval
        if not self.root.is_dir():
            raise ValueError("project folder does not exist")

    def _event(self, ref: KandevTaskRef, kind: str, payload: dict) -> None:
        self.store.add_event(ref.run_id, kind, payload, ref.task_id)
        if self.on_event:
            self.on_event(kind, payload)

    def _record(self, snapshot: KandevTaskSnapshot) -> OrchestrationResult:
        ref = snapshot.ref
        state = snapshot.state
        detail = snapshot.detail or state
        saved = self.store.get_run(ref.run_id)
        accepted = self.store.get_task_completion(ref.task_id)
        if saved and saved["status"] in {"ready_to_apply", "completed"} and accepted is not None:
            if state in {"ready", "running"}:
                self._event(ref, "kandev_stale_status_ignored", {
                    "remote_state": state, "accepted_checkpoint": accepted.revision,
                })
                return OrchestrationResult(
                    ref.run_id, str(saved["status"]), 1, 1, "",
                    Path(str(saved["worktree"])),
                    self.store.list_run_tasks(ref.run_id)[0].branch,
                    str(saved["base_commit"]), accepted.revision,
                )
        local_state = state if state in self.store.TASK_STATES else "review_required"
        if state == "reconciliation_required":
            local_state = "review_required"
        if state == "failed":
            self._event(ref, "kandev_failure_review_required", {
                "remote_task_id": ref.remote_task_id,
                "reason": "Remote execution failed; reconcile before any retry or internal fallback",
            })
        self.store.update_run_task(ref.task_id, local_state, 0, reason=snapshot.detail or f"Kandev reported {state}")
        self._event(ref, "kandev_task_observed", {
            "external_id": ref.external_id, "remote_task_id": ref.remote_task_id,
            "session_id": snapshot.session_id, "state": state,
            "worktree": snapshot.worktree, "observed_at": snapshot.observed_at,
        })
        accepted = False
        if state == "verifying":
            selected_base = str((self.store.get_run(ref.run_id) or {}).get("base_commit") or "")
            # Kandev 0.97.0's live task-session response omits base_commit_sha.
            # Use the base we recorded before launch, then prove ancestry in Git.
            if not snapshot.base_commit_sha and selected_base:
                snapshot = replace(snapshot, base_commit_sha=selected_base)
            expected_repository_id = str(getattr(getattr(self.adapter.api, "config", None), "repository_id", ""))
            check = verify_kandev_worktree(
                self.root, snapshot, expected_repository_id=expected_repository_id,
            )
            if check.valid and check.base_commit != selected_base:
                detail = "Kandev worktree base differs from the selected project revision"
                self._event(ref, "kandev_worktree_review_required", {"reason": detail})
            elif check.valid:
                self.store.update_task_workspace(
                    ref.task_id, worktree=check.path, branch=check.branch,
                    base_commit=check.base_commit,
                )
                self.store.update_run_state(ref.run_id, "needs_attention", worktree=check.path,
                                            base_commit=check.base_commit, final_commit=check.checkpoint)
                self._event(ref, "kandev_worktree_reconciled", {
                    "worktree": check.path, "branch": check.branch,
                    "base_commit": check.base_commit, "checkpoint": check.checkpoint,
                })
                if self.validate is not None:
                    accepted, detail = self._validate_checkpoint(ref, snapshot, check)
            else:
                detail = check.reason
                self._event(ref, "kandev_worktree_review_required", {"reason": check.reason})
        # Remote completion still needs MoliCoder's worktree and acceptance checks.
        if accepted:
            return OrchestrationResult(
                ref.run_id, "ready_to_apply", 1, 1, "",
                Path(snapshot.worktree), snapshot.worktree_branch,
                snapshot.base_commit_sha, check.checkpoint,
            )
        run_state = "running" if state in {"ready", "running"} else "needs_attention"
        self.store.update_run_state(ref.run_id, run_state, recovery_reason=detail)
        return OrchestrationResult(ref.run_id, run_state, 0, 1, detail)

    def _validate_checkpoint(self, ref: KandevTaskRef, snapshot: KandevTaskSnapshot, check) -> tuple[bool, str]:
        worktree = Path(check.path)
        try:
            result = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--name-only", check.base_commit, check.checkpoint, "--"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, "Kandev changed paths could not be verified"
        if result.returncode != 0:
            return False, "Kandev changed paths could not be verified"
        changed = tuple(sorted({line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}))
        if not changed:
            return False, "Kandev checkpoint has no changed files"
        architecture = assess_architectural_invariants(self.store, self.project.id, changed)
        if architecture.conflicts or architecture.review_required:
            self._event(ref, "kandev_architecture_review_required", {"changed_paths": changed})
            return False, "Kandev checkpoint requires architectural review"
        try:
            summary = self.validate(worktree) if self.validate is not None else ""
        except Exception:
            return False, "Kandev checkpoint validation failed"
        if not summary.lstrip().startswith("validation passed"):
            self._event(ref, "kandev_validation_failed", {"summary": summary[-8000:]})
            return False, summary[-8000:] or "Kandev checkpoint validation failed"
        rechecked = verify_kandev_worktree(
            self.root, snapshot,
            expected_repository_id=str(getattr(getattr(self.adapter.api, "config", None), "repository_id", "")),
        )
        if not rechecked.valid or rechecked.checkpoint != check.checkpoint:
            return False, "Kandev worktree changed during validation"
        existing = self.store.get_task_completion(ref.task_id)
        if existing is not None and existing.revision != check.checkpoint:
            return False, "Kandev checkpoint differs from the previously accepted revision"
        if existing is None:
            self.store.record_task_completion(
                ref.run_id, ref.task_id, revision=check.checkpoint,
                checks=("project-default",), validation_summary=summary,
                output_artifacts=changed, unmet_criteria=(), human_review_status="not_required",
            )
        self.store.update_task_workspace(
            ref.task_id, worktree=check.path, branch=check.branch,
            base_commit=check.base_commit, changed_paths=changed,
        )
        self.store.update_run_task(ref.task_id, "completed", 0, reason="Kandev checkpoint passed local validation")
        self.store.update_run_state(ref.run_id, "ready_to_apply", worktree=check.path,
                                    base_commit=check.base_commit, final_commit=check.checkpoint)
        self._event(ref, "kandev_checkpoint_accepted", {
            "checkpoint": check.checkpoint, "changed_paths": changed,
            "validation_summary": summary[-8000:],
        })
        return True, ""

    def run(self, request: str, *, mode: str = "HYBRID") -> OrchestrationResult:
        if not request.strip():
            raise ValueError("Build request is empty")
        if self.adapter.probe().health_state != "ready":
            raise RuntimeError("Kandev task capabilities are incomplete")
        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(self.root), *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        head = git("rev-parse", "HEAD")
        branch = git("branch", "--show-current")
        dirty = git("status", "--porcelain", "--untracked-files=all")
        if (head.returncode != 0 or branch.returncode != 0 or not branch.stdout.strip()
                or dirty.returncode != 0 or dirty.stdout.strip()):
            raise ValueError("Kandev Build requires a clean branch with a base commit")
        base = head.stdout.strip()
        run_id = self.store.create_run(
            self.project.id, request, mode, provider="kandev", base_commit=base,
        )
        task_id = self.store.create_run_task(run_id, "K1", "Kandev Build task", request, "high")
        ref = KandevTaskRef(run_id, task_id, f"molicoder:project:{self.project.id}:run:{run_id}:task:{task_id}")
        if self.require_plan_approval:
            plan = parse_plan({"objective": request, "tasks": [{
                "key": "K1", "title": "Kandev Build task", "objective": request,
                "risk": "high", "depends_on": [],
                "acceptance_criteria": ["Configured validation must pass"],
                "verification_checks": ["project-default"],
            }]})
            plan_payload = plan.to_dict() | {"runner_policy": {
                "backend": "kandev", "endpoint": self.adapter.connection.endpoint,
                "agent_profile_id": str(getattr(getattr(self.adapter.api, "config", None), "agent_profile_id", "")),
                "model_assignment": "configured Kandev agent profile; per-task model selection unverified",
            }}
            revision = self.store.save_plan(run_id, "generated", "kandev", plan_payload)
            self.store.update_run_state(run_id, "awaiting_approval")
            self._event(ref, "plan_awaiting_approval", {
                "revision": revision, "base_commit": base, "task_keys": ["K1"],
                "runner": "kandev", "model_assignment": "configured Kandev agent profile",
            })
            return OrchestrationResult(run_id, "awaiting_approval", 0, 1)
        return self._launch(ref, request, branch.stdout.strip(), base)

    def execute_approved(self, run_id: int) -> OrchestrationResult:
        run, plan = require_approved_plan(self.store, run_id)
        if (int(run["project_id"]) != self.project.id or run["status"] != "awaiting_approval"
                or run["provider"] != "kandev"):
            raise PermissionError("Kandev Build is not awaiting approval for this project")
        tasks = self.store.list_run_tasks(run_id)
        if len(tasks) != 1 or tasks[0].key != "K1":
            raise ValueError("Kandev plan task identity differs")
        approved_plan = parse_plan(plan["payload"])
        if (len(approved_plan.tasks) != 1 or approved_plan.objective != run["request"]
                or approved_plan.tasks[0].key != tasks[0].key
                or approved_plan.tasks[0].objective != tasks[0].objective
                or approved_plan.tasks[0].depends_on):
            raise PermissionError("Approved Kandev plan differs from the recorded task")
        policy = plan["payload"].get("runner_policy", {})
        if (policy.get("backend") != "kandev"
                or policy.get("endpoint") != self.adapter.connection.endpoint
                or policy.get("agent_profile_id") != str(getattr(getattr(self.adapter.api, "config", None), "agent_profile_id", ""))):
            raise PermissionError("Approved Kandev runner policy changed")
        if self.adapter.probe().health_state != "ready":
            raise RuntimeError("Kandev is unavailable; the approved plan remains pending")
        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(self.root), *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        head = git("rev-parse", "HEAD")
        branch = git("branch", "--show-current")
        dirty = git("status", "--porcelain", "--untracked-files=all")
        if (head.returncode != 0 or head.stdout.strip() != run["base_commit"]
                or branch.returncode != 0 or not branch.stdout.strip()
                or dirty.returncode != 0 or dirty.stdout.strip()):
            raise PermissionError("Project changed after Kandev plan approval")
        ref = KandevTaskRef(run_id, tasks[0].id,
                            f"molicoder:project:{self.project.id}:run:{run_id}:task:{tasks[0].id}")
        self.store.update_run_state(run_id, "scheduling")
        return self._launch(ref, str(run["request"]), branch.stdout.strip(), str(run["base_commit"]))

    def _launch(self, ref: KandevTaskRef, request: str, branch: str, base: str) -> OrchestrationResult:
        self._event(ref, "kandev_owner_selected", {
            "external_id": ref.external_id, "connection_mode": self.adapter.connection.mode.value,
            "endpoint": self.adapter.connection.endpoint, "project_branch": branch,
            "base_commit": base,
        })
        try:
            if self.authorize_launch is not None:
                self.authorize_launch(ref)
        except (PermissionError, ValueError) as exc:
            self.store.update_run_task(ref.task_id, "waiting_approval", 0, reason=str(exc))
            self.store.update_run_state(ref.run_id, "needs_attention", recovery_reason=str(exc))
            self._event(ref, "kandev_launch_blocked", {"reason": str(exc)})
            return OrchestrationResult(ref.run_id, "needs_attention", 0, 1, str(exc))
        try:
            snapshot = self.adapter.create_task(ref, request)
        except PermissionError as exc:
            self.store.update_run_task(ref.task_id, "waiting_approval", 0, reason=str(exc))
            self.store.update_run_state(ref.run_id, "needs_attention", recovery_reason=str(exc))
            self._event(ref, "kandev_launch_blocked", {"reason": str(exc)})
            return OrchestrationResult(ref.run_id, "needs_attention", 0, 1, str(exc))
        except Exception:
            # The create may have reached Kandev. Keep the local task bound to
            # its external ID, requiring reconciliation instead of fallback.
            self.store.update_run_task(ref.task_id, "review_required", 0, reason="Kandev create outcome needs reconciliation")
            self.store.update_run_state(ref.run_id, "needs_attention", recovery_reason="Kandev create outcome needs reconciliation")
            self._event(ref, "kandev_reconciliation_required", {"external_id": ref.external_id})
            return OrchestrationResult(ref.run_id, "needs_attention", 0, 1, "Kandev create outcome needs reconciliation")
        return self._record(snapshot)

    def resume(self, run_id: int) -> OrchestrationResult:
        run = self.store.get_run(run_id)
        if run is None or run["project_id"] != self.project.id or run["provider"] != "kandev":
            raise ValueError("run is not a Kandev-owned Build for this project")
        tasks = self.store.list_run_tasks(run_id)
        if len(tasks) != 1:
            raise ValueError("Kandev-owned Build has an invalid local task record")
        if run["status"] == "completed":
            return OrchestrationResult(
                run_id, "completed", 1, 1, "", Path(str(run["worktree"])),
                tasks[0].branch, str(run["base_commit"]), str(run["final_commit"]),
            )
        task_id = tasks[0].id
        selection = next((json.loads(event.payload_json) for event in self.store.list_events(run_id)
                          if event.task_id == task_id and event.kind == "kandev_owner_selected"), None)
        if not isinstance(selection, dict):
            raise ValueError("Kandev ownership record is missing")
        expected = f"molicoder:project:{self.project.id}:run:{run_id}:task:{task_id}"
        if selection.get("external_id") != expected or selection.get("endpoint") != self.adapter.connection.endpoint:
            raise ValueError("Kandev ownership identity or endpoint differs")
        if selection.get("base_commit") != run["base_commit"]:
            raise ValueError("Kandev base commit selection differs")
        ref = KandevTaskRef(run_id, task_id, expected)
        try:
            snapshot = self.adapter.reconcile(ref)
        except KandevTransportError as exc:
            reason = f"Kandev connection failed during reconciliation: {exc}"
            self.store.update_run_state(run_id, "needs_attention", recovery_reason=reason)
            self._event(ref, "kandev_reconnect_required", {
                "external_id": ref.external_id, "reason": reason,
            })
            return OrchestrationResult(run_id, "needs_attention", 0, 1, reason)
        if snapshot.ref.remote_task_id:
            self.adapter.subscribe_events(snapshot.ref)
        return self._record(snapshot)

    def apply(self, run_id: int) -> OrchestrationResult:
        """Fast-forward a locally accepted Kandev checkpoint once."""
        run = self.store.get_run(run_id)
        if run is None or run["project_id"] != self.project.id or run["provider"] != "kandev":
            raise ValueError("run is not a Kandev-owned Build for this project")
        if run["status"] not in {"ready_to_apply", "completed"}:
            raise ValueError("Kandev checkpoint has not passed local acceptance")
        tasks = self.store.list_run_tasks(run_id)
        if len(tasks) != 1 or tasks[0].status != "completed":
            raise ValueError("Kandev task acceptance record is missing")
        task = tasks[0]
        completion = self.store.get_task_completion(task.id)
        checkpoint = str(run["final_commit"] or "")
        base = str(run["base_commit"] or "")
        if (completion is None or completion.revision != checkpoint
                or task.worktree != run["worktree"] or task.base_commit != base):
            raise ValueError("Kandev accepted checkpoint identity differs")
        selection = next((json.loads(event.payload_json) for event in self.store.list_events(run_id)
                          if event.task_id == task.id and event.kind == "kandev_owner_selected"), None)
        if (not isinstance(selection, dict) or selection.get("endpoint") != self.adapter.connection.endpoint
                or selection.get("base_commit") != base):
            raise ValueError("Kandev ownership selection differs")
        ref = KandevTaskRef(
            run_id, task.id, f"molicoder:project:{self.project.id}:run:{run_id}:task:{task.id}",
        )
        snapshot = self.adapter.reconcile(ref)
        if not snapshot.base_commit_sha:
            snapshot = replace(snapshot, base_commit_sha=base)
        check = verify_kandev_worktree(
            self.root, snapshot,
            expected_repository_id=str(getattr(getattr(self.adapter.api, "config", None), "repository_id", "")),
        )
        if (snapshot.state != "verifying" or not check.valid or check.path != task.worktree
                or check.base_commit != base or check.checkpoint != checkpoint):
            raise ValueError("Kandev worktree changed after acceptance")
        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(self.root), *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        branch = git("branch", "--show-current")
        head = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=all")
        if (branch.returncode != 0 or not branch.stdout.strip() or head.returncode != 0
                or dirty.returncode != 0 or dirty.stdout.strip()):
            raise ValueError("project checkout changed or is dirty")
        if branch.stdout.strip() != selection.get("project_branch"):
            raise ValueError("project branch changed after the Kandev task started")
        current = head.stdout.strip()
        if current != checkpoint:
            if current != base:
                raise ValueError("project HEAD changed after the Kandev task started")
            merged = git("merge", "--ff-only", checkpoint)
            if merged.returncode != 0 or git("rev-parse", "HEAD").stdout.strip() != checkpoint:
                raise ValueError("Kandev checkpoint could not be fast-forwarded")
        if run["status"] != "completed":
            self.store.finish_run(run_id, "completed")
            self._event(ref, "kandev_changes_applied", {
                "base_commit": base, "final_commit": checkpoint,
                "worktree": check.path, "branch": check.branch,
            })
        return OrchestrationResult(run_id, "completed", 1, 1, "", Path(check.path),
                                   check.branch, base, checkpoint)
