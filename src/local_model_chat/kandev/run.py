from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..domain import Project
from ..molicoder.orchestrator import OrchestrationResult
from ..storage import ChatStore
from .adapter import KandevTaskAdapter
from .contracts import KandevTaskRef, KandevTaskSnapshot
from .worktree import verify_kandev_worktree


class KandevRunOrchestrator:
    """Submit one Build request to Kandev and retain its remote ownership."""

    def __init__(
        self, *, store: ChatStore, project: Project, adapter: KandevTaskAdapter,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.store = store
        self.project = project
        self.root = Path(project.path).resolve()
        self.adapter = adapter
        self.on_event = on_event
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
        local_state = state if state in self.store.TASK_STATES else "review_required"
        if state == "reconciliation_required":
            local_state = "review_required"
        self.store.update_run_task(ref.task_id, local_state, 0, reason=snapshot.detail or f"Kandev reported {state}")
        self._event(ref, "kandev_task_observed", {
            "external_id": ref.external_id, "remote_task_id": ref.remote_task_id,
            "session_id": snapshot.session_id, "state": state,
            "worktree": snapshot.worktree, "observed_at": snapshot.observed_at,
        })
        if state == "verifying":
            expected_repository_id = str(getattr(getattr(self.adapter.api, "config", None), "repository_id", ""))
            check = verify_kandev_worktree(
                self.root, snapshot, expected_repository_id=expected_repository_id,
            )
            if check.valid:
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
            else:
                detail = check.reason
                self._event(ref, "kandev_worktree_review_required", {"reason": check.reason})
        # Remote completion still needs MoliCoder's worktree and acceptance checks.
        run_state = "running" if state in {"ready", "running"} else "needs_attention"
        self.store.update_run_state(ref.run_id, run_state, recovery_reason=detail)
        return OrchestrationResult(ref.run_id, run_state, 0, 1, detail)

    def run(self, request: str, *, mode: str = "HYBRID") -> OrchestrationResult:
        if not request.strip():
            raise ValueError("Build request is empty")
        if self.adapter.probe().health_state != "ready":
            raise RuntimeError("Kandev task capabilities are incomplete")
        run_id = self.store.create_run(self.project.id, request, mode, provider="kandev")
        task_id = self.store.create_run_task(run_id, "K1", "Kandev Build task", request, "high")
        ref = KandevTaskRef(run_id, task_id, f"molicoder:project:{self.project.id}:run:{run_id}:task:{task_id}")
        self._event(ref, "kandev_owner_selected", {
            "external_id": ref.external_id, "connection_mode": self.adapter.connection.mode.value,
            "endpoint": self.adapter.connection.endpoint,
        })
        try:
            snapshot = self.adapter.create_task(ref, request)
        except Exception:
            # The create may have reached Kandev. Keep the local task bound to
            # its external ID, requiring reconciliation instead of fallback.
            self.store.update_run_task(task_id, "review_required", 0, reason="Kandev create outcome needs reconciliation")
            self.store.update_run_state(run_id, "needs_attention", recovery_reason="Kandev create outcome needs reconciliation")
            self._event(ref, "kandev_reconciliation_required", {"external_id": ref.external_id})
            return OrchestrationResult(run_id, "needs_attention", 0, 1, "Kandev create outcome needs reconciliation")
        return self._record(snapshot)

    def resume(self, run_id: int) -> OrchestrationResult:
        run = self.store.get_run(run_id)
        if run is None or run["project_id"] != self.project.id or run["provider"] != "kandev":
            raise ValueError("run is not a Kandev-owned Build for this project")
        tasks = self.store.list_run_tasks(run_id)
        if len(tasks) != 1:
            raise ValueError("Kandev-owned Build has an invalid local task record")
        task_id = tasks[0].id
        selection = next((json.loads(event.payload_json) for event in self.store.list_events(run_id)
                          if event.task_id == task_id and event.kind == "kandev_owner_selected"), None)
        if not isinstance(selection, dict):
            raise ValueError("Kandev ownership record is missing")
        expected = f"molicoder:project:{self.project.id}:run:{run_id}:task:{task_id}"
        if selection.get("external_id") != expected or selection.get("endpoint") != self.adapter.connection.endpoint:
            raise ValueError("Kandev ownership identity or endpoint differs")
        ref = KandevTaskRef(run_id, task_id, expected)
        snapshot = self.adapter.reconcile(ref)
        if snapshot.ref.remote_task_id:
            self.adapter.subscribe_events(snapshot.ref)
        return self._record(snapshot)
