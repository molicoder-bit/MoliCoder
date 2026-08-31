from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ProjectState:
    root: Path
    kind: str
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GitState:
    branch: str
    dirty: bool
    available: bool


_VALIDATION: dict[str, list[dict[str, Any]]] = {
    "python": [{"name": "pytest", "argv": ["{python}", "-m", "pytest"]}],
    "node": [
        {"name": "test", "argv": ["npm", "test", "--", "--runInBand"]},
        {"name": "build", "argv": ["npm", "run", "build"]},
    ],
    "flutter": [
        {"name": "analyze", "argv": ["flutter", "analyze"]},
        {"name": "test", "argv": ["flutter", "test"]},
    ],
    "unknown": [],
}


def detect_project_kind(root: str | Path) -> str:
    path = Path(root).resolve()
    if (path / "pubspec.yaml").is_file():
        return "flutter"
    if (path / "package.json").is_file():
        return "node"
    if (path / "pyproject.toml").is_file() or (path / "pytest.ini").is_file():
        return "python"
    return "unknown"


def ensure_project_state(root: str | Path) -> ProjectState:
    path = Path(root).resolve()
    if not path.is_dir():
        raise ValueError(f"project folder does not exist: {path}")
    state_dir = path / ".molicoder"
    state_dir.mkdir(parents=True, exist_ok=True)
    for child in ("plans", "tasks", "runs", "logs", "failures", "skills"):
        (state_dir / child).mkdir(exist_ok=True)
    config_path = state_dir / "project.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"invalid MoliCoder project config: {config_path}") from exc
        if not isinstance(config, dict) or not isinstance(config.get("kind"), str):
            raise ValueError(f"invalid MoliCoder project config: {config_path}")
    else:
        kind = detect_project_kind(path)
        config = {
            "version": 1,
            "kind": kind,
            "validation": _VALIDATION[kind],
            "constraints": [],
        }
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="")
    instructions = state_dir / "instructions.md"
    if not instructions.exists():
        instructions.write_text(
            "# MoliCoder project instructions\n\nAdd project-specific rules here.\n",
            encoding="utf-8",
            newline="",
        )
    ignore = state_dir / ".gitignore"
    ignored = ("logs/", "plans/", "tasks/", "runs/", "failures/", "worktrees/")
    existing = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
    missing = [entry for entry in ignored if entry not in existing]
    if missing:
        ignore.write_text("\n".join(existing + missing) + "\n", encoding="utf-8", newline="")
    return ProjectState(path, str(config["kind"]), config)


def read_git_state(root: str | Path) -> GitState:
    path = Path(root).resolve()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    probe = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=creationflags,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return GitState("Not a Git repository", False, False)
    branch = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=creationflags,
    ).stdout.strip() or "Detached HEAD"
    dirty = bool(
        subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=creationflags,
        ).stdout.strip()
    )
    return GitState(branch, dirty, True)
