"""Read-only reconciliation evidence for interrupted internal Build runs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..storage import ChatStore


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False, timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve())) == os.path.normcase(str(second.resolve()))


def observe_interrupted_run(store: ChatStore, run_id: int) -> dict[str, object]:
    """Describe the actual worktree without accepting or replaying any work."""
    run = store.get_run(run_id)
    if run is None or run["provider"] == "kandev":
        raise ValueError("only a persisted internal Build can be observed")
    project = store.get_project(int(run["project_id"]))
    if project is None:
        return {"workspace_state": "project_missing", "accepted_tasks": 0}
    root = Path(project.path).resolve()
    path_value = str(run.get("worktree") or "")
    if not path_value:
        return {"workspace_state": "workspace_missing", "accepted_tasks": 0}
    workspace = Path(path_value).resolve()
    if not workspace.is_dir():
        return {"workspace_state": "workspace_missing", "accepted_tasks": 0}
    try:
        registered = _git(root, "worktree", "list", "--porcelain")
        if registered.returncode != 0 or not any(
            line.startswith("worktree ") and _same_path(Path(line[9:]), workspace)
            for line in registered.stdout.splitlines()
        ):
            return {"workspace_state": "workspace_unregistered", "accepted_tasks": 0}
        top = _git(workspace, "rev-parse", "--show-toplevel")
        branch = _git(workspace, "branch", "--show-current")
        head = _git(workspace, "rev-parse", "HEAD")
        status = _git(workspace, "status", "--porcelain", "--untracked-files=all")
        if any(result.returncode != 0 for result in (top, branch, head, status)):
            return {"workspace_state": "git_unavailable", "accepted_tasks": 0}
        checkpoint = head.stdout.strip()
        if (not _same_path(Path(top.stdout.strip()), workspace)
                or branch.stdout.strip() != f"molicoder/run-{run_id}"):
            return {"workspace_state": "identity_mismatch", "accepted_tasks": 0}
        base = str(run.get("base_commit") or "")
        if not base or _git(workspace, "merge-base", "--is-ancestor", base, checkpoint).returncode != 0:
            return {"workspace_state": "base_mismatch", "checkpoint": checkpoint, "accepted_tasks": 0}
        tasks = store.list_run_tasks(run_id)
        accepted = 0
        for task in tasks:
            completion = store.get_task_completion(task.id)
            if completion is None:
                continue
            if _git(workspace, "merge-base", "--is-ancestor", completion.revision, checkpoint).returncode != 0:
                return {
                    "workspace_state": "accepted_checkpoint_missing", "checkpoint": checkpoint,
                    "accepted_tasks": accepted,
                }
            accepted += 1
        dirty = bool(status.stdout.strip())
        return {
            "workspace_state": "dirty" if dirty else (
                "clean_unverified" if accepted < len(tasks) else "clean"
            ),
            "checkpoint": checkpoint, "accepted_tasks": accepted,
            "unfinished_tasks": len(tasks) - accepted, "dirty": dirty,
        }
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"workspace_state": "inspection_unavailable", "accepted_tasks": 0}


def recover_interrupted_builds(store: ChatStore) -> tuple[int, ...]:
    """Move dead-owner runs to review and record observed Git state once."""
    run_ids = store.recover_interrupted_runs()
    for run_id in run_ids:
        observed = observe_interrupted_run(store, run_id)
        store.add_event(run_id, "run_recovery_observed", observed)
    return run_ids
