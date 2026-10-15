from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .domain import Project


@dataclass(frozen=True, slots=True)
class GitQueryResult:
    content: str
    evidence: str


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Git is unavailable: {exc}") from exc
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git could not read this project.")
    return result.stdout.rstrip("\r\n")


def query_git(project: Project, operation: str) -> GitQueryResult:
    root = Path(project.path)
    if not root.is_dir():
        raise ValueError(f"The project folder is missing: {root}")
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    if operation == "git_version":
        branch = _git(root, "branch", "--show-current") or "detached HEAD"
        commit = _git(root, "rev-parse", "--short", "HEAD")
        content = (
            f"**{project.name}** is on branch **{branch}** at commit `{commit}`.\n\n"
            f"Checked: {timestamp}. Source: local Git repository `{root}`."
        )
        return GitQueryResult(content, f"git branch --show-current; git rev-parse --short HEAD in {root}")
    if operation == "changed_files":
        changes = _git(root, "status", "--short", "--untracked-files=normal")
        lines = changes.splitlines()
        if not lines:
            detail = "No changed files are reported by Git."
        else:
            shown = "\n".join(f"- `{line}`" for line in lines[:50])
            remainder = f"\n…and {len(lines) - 50} more." if len(lines) > 50 else ""
            detail = f"{len(lines)} changed file entries:\n{shown}{remainder}"
        content = (
            f"**{project.name}**: {detail}\n\n"
            f"Checked: {timestamp}. Source: local `git status --short` in `{root}`."
        )
        return GitQueryResult(content, f"git status --short in {root}")
    raise ValueError(f"Unsupported Git query: {operation}")
