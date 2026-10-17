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

    def create_run(self) -> TaskWorkspace:
        """Create the single sequential workspace used by modern Build runs."""
        return self._create("workspace", f"molicoder/run-{self.run_id}")

    def create_integration(self) -> TaskWorkspace:
        return self._create("integration", f"molicoder/run-{self.run_id}/integration")

    def _stage_changes(self, workspace: Path) -> None:
        # Validation must never turn interpreter/test caches into task output,
        # even when an imported repository has no useful .gitignore yet.
        tracked = self._git("diff", "--name-only", "-z", "HEAD", "--", cwd=workspace).stdout
        unmerged = self._git(
            "diff", "--name-only", "--diff-filter=U", "-z", "--", cwd=workspace, check=False
        ).stdout
        untracked = self._git(
            "ls-files", "--others", "--exclude-standard", "-z", "--", cwd=workspace
        ).stdout
        candidates = {path for path in (tracked + unmerged + untracked).split("\0") if path}
        paths = []
        for raw in sorted(candidates):
            path = raw.replace("\\", "/")
            parts = set(Path(path).parts)
            if "__pycache__" in parts or ".pytest_cache" in parts or path.endswith((".pyc", ".pyo")):
                continue
            paths.append(raw)
        if paths:
            self._git("add", "-A", "--", *paths, cwd=workspace)

    def commit(self, workspace: TaskWorkspace, message: str) -> tuple[str, ...]:
        self._stage_changes(workspace.path)
        names = self._git("diff", "--cached", "--name-only", "--", cwd=workspace.path).stdout.splitlines()
        paths = tuple(sorted(path.strip().replace("\\", "/") for path in names if path.strip()))
        if not paths:
            return ()
        self._git(
            "-c", "user.name=MoliCoder", "-c", "user.email=molicoder@localhost",
            "commit", "-m", message[:200], cwd=workspace.path,
        )
        return paths

    def preserve_failed_attempt(self, workspace: TaskWorkspace, label: str) -> str | None:
        """Save tracked and untracked attempt edits, then leave the accepted HEAD clean."""
        path = workspace.path.resolve()
        base = self.base_dir.resolve()
        if not path.is_relative_to(base) or path == base:
            raise GitOperationError("refusing to clean a path outside the managed worktree root")
        top = Path(self._git("rev-parse", "--show-toplevel", cwd=path).stdout.strip()).resolve()
        if top != path:
            raise GitOperationError("retry workspace is not the expected Git root")
        ignored = self._git("status", "--porcelain", "--ignored", "--untracked-files=all", cwd=path).stdout
        ignored_outputs = [
            line[3:].replace("\\", "/") for line in ignored.splitlines()
            if line.startswith("!! ") and line[3:].replace("\\", "/") not in {".molicoder", ".molicoder/"}
            and not line[3:].replace("\\", "/").startswith(".molicoder/")
        ]
        # Test/interpreter caches are generated by our validation too. Preserve
        # them in the failed-attempt stash rather than blocking a model handoff
        # or letting stale bytecode / last-failed state affect the next attempt.
        cache_outputs = [raw for raw in ignored_outputs
                         if {'__pycache__', '.pytest_cache'} & set(Path(raw).parts)
                         or raw.endswith(('.pyc', '.pyo'))]
        unknown_outputs = [raw for raw in ignored_outputs if raw not in cache_outputs]
        if unknown_outputs:
            raise GitOperationError(
                "ignored attempt output requires review before retry: " + ", ".join(unknown_outputs[:5])
            )
        before = self._git("status", "--porcelain", "--untracked-files=all", cwd=path).stdout.strip()
        if not before and not cache_outputs:
            return None
        result = self._git("stash", "push", "--all", "-m", label[:120],
                           "--", ".", ":(exclude).molicoder", cwd=path, check=False)
        if result.returncode != 0:
            raise GitOperationError((result.stdout + result.stderr).strip() or "failed attempt snapshot failed")
        after = self._git("status", "--porcelain", "--untracked-files=all", cwd=path).stdout.strip()
        if after:
            raise GitOperationError("failed attempt snapshot left changes in the retry workspace")
        return self._git("rev-parse", "refs/stash", cwd=path).stdout.strip()

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
        self._stage_changes(integration.path)
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

    def can_fast_forward_checkout(self, workspace: TaskWorkspace) -> tuple[bool, str]:
        head = self._git("rev-parse", "HEAD", check=False).stdout.strip()
        if head != workspace.base_commit:
            return False, "project HEAD changed while the run was active"
        branch = self._git("branch", "--show-current", check=False).stdout.strip()
        if not branch:
            return False, "project checkout is detached"
        dirty = self._git("status", "--porcelain", check=False).stdout.strip()
        if dirty:
            return False, "project checkout changed while the run was active"
        return True, ""

    def fast_forward_checkout(self, workspace: TaskWorkspace) -> str:
        allowed, reason = self.can_fast_forward_checkout(workspace)
        if not allowed:
            raise GitOperationError(reason)
        result = self._git("merge", "--ff-only", workspace.branch, check=False)
        if result.returncode != 0:
            raise GitOperationError((result.stdout + result.stderr).strip() or "fast-forward failed")
        return self._git("rev-parse", "HEAD").stdout.strip()
