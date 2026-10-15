from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .contracts import KandevTaskSnapshot


@dataclass(frozen=True, slots=True)
class KandevWorktreeCheck:
    valid: bool
    path: str = ""
    branch: str = ""
    base_commit: str = ""
    checkpoint: str = ""
    reason: str = ""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Git worktree inspection is unavailable") from exc


def verify_kandev_worktree(
    project_root: Path, snapshot: KandevTaskSnapshot, *, expected_repository_id: str,
) -> KandevWorktreeCheck:
    """Accept only a clean registered worktree from the selected repository."""
    if not expected_repository_id or snapshot.repository_id != expected_repository_id:
        return KandevWorktreeCheck(False, reason="Kandev repository identity differs")
    if not snapshot.worktree or not snapshot.worktree_branch or not snapshot.base_commit_sha:
        return KandevWorktreeCheck(False, reason="Kandev worktree identity is incomplete")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", snapshot.base_commit_sha):
        return KandevWorktreeCheck(False, reason="Kandev base commit is invalid")
    root = project_root.resolve()
    worktree = Path(snapshot.worktree).resolve()
    if not root.is_dir() or not worktree.is_dir() or worktree == root:
        return KandevWorktreeCheck(False, reason="Kandev worktree path is unavailable")
    listed = _git(root, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        return KandevWorktreeCheck(False, reason="project Git worktree list is unavailable")
    registered = {Path(line[9:]).resolve() for line in listed.stdout.splitlines() if line.startswith("worktree ")}
    if worktree not in registered:
        return KandevWorktreeCheck(False, reason="Kandev worktree is not registered by the project")
    top = _git(worktree, "rev-parse", "--show-toplevel")
    branch = _git(worktree, "branch", "--show-current")
    head = _git(worktree, "rev-parse", "HEAD")
    if (top.returncode != 0 or Path(top.stdout.strip()).resolve() != worktree
            or branch.returncode != 0 or branch.stdout.strip() != snapshot.worktree_branch
            or head.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head.stdout.strip())):
        return KandevWorktreeCheck(False, reason="Kandev worktree branch or Git root differs")
    base = _git(worktree, "merge-base", "--is-ancestor", snapshot.base_commit_sha, "HEAD")
    if base.returncode != 0:
        return KandevWorktreeCheck(False, reason="Kandev base commit is not an ancestor")
    if head.stdout.strip() == snapshot.base_commit_sha:
        return KandevWorktreeCheck(False, reason="Kandev worktree has no committed change")
    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0 or status.stdout.strip():
        return KandevWorktreeCheck(False, reason="Kandev worktree has uncommitted changes")
    return KandevWorktreeCheck(
        True, str(worktree), snapshot.worktree_branch,
        snapshot.base_commit_sha, head.stdout.strip(),
    )
