from __future__ import annotations

import re
import hashlib
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from ..config import data_dir


class GitOperationError(RuntimeError):
    pass


class IntegrationConflict(GitOperationError):
    def __init__(self, paths: tuple[str, ...], output: str) -> None:
        super().__init__(f"merge conflict in: {', '.join(paths) or 'unknown paths'}")
        self.paths = paths
        self.output = output


@dataclass(frozen=True, slots=True)
class TaskWorkspace:
    key: str
    path: Path
    branch: str
    base_commit: str


def _safe(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned[:60] or "task"


class WorktreeManager:
    def __init__(self, root: str | Path, *, run_id: int, base_dir: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.run_id = int(run_id)
        root_identity = hashlib.sha256(str(self.root).casefold().encode("utf-8")).hexdigest()[:12]
        project_directory = f"{_safe(self.root.name)}-{root_identity}"
        self.base_dir = Path(base_dir or (data_dir() / "worktrees" / project_directory)).resolve() / f"run-{run_id}"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._admin_lock = threading.Lock()
        self.base_commit = self._git("rev-parse", "HEAD").stdout.strip()
        if not self.base_commit:
            raise GitOperationError("repository has no base commit")

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode != 0:
            raise GitOperationError((result.stdout + result.stderr).strip())
        return result

    def _create(self, key: str, branch: str) -> TaskWorkspace:
        path = self.base_dir / _safe(key)
        if path.exists():
            raise GitOperationError(f"worktree path already exists: {path}")
        with self._admin_lock:
            self._git("worktree", "add", "-b", branch, str(path), self.base_commit)
        return TaskWorkspace(key, path, branch, self.base_commit)

    def create_task(self, key: str) -> TaskWorkspace:
        safe_key = _safe(key)
        return self._create(safe_key, f"molicoder/run-{self.run_id}/{safe_key}")

    def create_integration(self) -> TaskWorkspace:
        return self._create("integration", f"molicoder/run-{self.run_id}/integration")

    def commit(self, workspace: TaskWorkspace, message: str) -> tuple[str, ...]:
        self._git("add", "-A", cwd=workspace.path)
        names = self._git("diff", "--cached", "--name-only", "--", cwd=workspace.path).stdout.splitlines()
        paths = tuple(sorted(path.strip().replace("\\", "/") for path in names if path.strip()))
        if not paths:
            return ()
        self._git(
            "-c", "user.name=MoliCoder", "-c", "user.email=molicoder@localhost",
            "commit", "-m", message[:200], cwd=workspace.path,
        )
        return paths

    def merge_into(
        self, integration: TaskWorkspace, task: TaskWorkspace, *, preserve_conflict: bool = False
    ) -> None:
        result = self._git(
            "-c", "user.name=MoliCoder", "-c", "user.email=molicoder@localhost",
            "merge", "--no-ff", "--no-edit", task.branch, cwd=integration.path, check=False,
        )
        if result.returncode == 0:
            return
        paths = tuple(
            sorted(
                path.strip().replace("\\", "/")
                for path in self._git("diff", "--name-only", "--diff-filter=U", cwd=integration.path, check=False).stdout.splitlines()
                if path.strip()
            )
        )
        output = (result.stdout + result.stderr).strip()
        if not preserve_conflict:
            self._git("merge", "--abort", cwd=integration.path, check=False)
        raise IntegrationConflict(paths, output)

    def complete_conflict_merge(self, integration: TaskWorkspace, paths: tuple[str, ...]) -> tuple[str, ...]:
        for relative in paths:
            target = integration.path / relative
            if target.is_file():
                content = target.read_text(encoding="utf-8", errors="replace")
                if "<<<<<<<" in content or "=======" in content or ">>>>>>>" in content:
                    raise IntegrationConflict(paths, "conflict markers remain after repair")
        self._git("add", "-A", cwd=integration.path)
        names = self._git(
            "diff", "--cached", "--name-only", "--", cwd=integration.path
        ).stdout.splitlines()
        changed_paths = tuple(
            sorted(path.strip().replace("\\", "/") for path in names if path.strip())
        )
        merge_head = self._git(
            "rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=integration.path, check=False
        )
        if merge_head.returncode != 0:
            raise GitOperationError("no integration merge is in progress")
        self._git(
            "-c", "user.name=MoliCoder", "-c", "user.email=molicoder@localhost",
            "commit", "--no-edit", cwd=integration.path,
        )
        return changed_paths

    def remove(self, workspace: TaskWorkspace) -> None:
        self._git("worktree", "remove", "--force", str(workspace.path))
