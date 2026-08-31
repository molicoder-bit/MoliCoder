from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .executor import AgentExecutor
from .gateway import InferenceGateway
from .tools import ToolDispatcher
from .validate import ProjectValidator, ValidationCommand


def _git_state(root: Path) -> tuple[str, bool]:
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=root, capture_output=True, text=True, check=False
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False
        ).stdout.strip()
    )
    return branch, dirty


def _python_executable(root: Path) -> str:
    candidates = [root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"]
    return str(next((path for path in candidates if path.is_file()), Path(sys.executable)))


def _validator(root: Path) -> ProjectValidator:
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file():
        return ProjectValidator(
            root,
            [ValidationCommand("pytest", (_python_executable(root), "-m", "pytest"))],
        )
    raise ValueError("Phase 1 currently supports Python/pytest projects only")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one bounded MoliCoder executor task.")
    parser.add_argument("task", help="A small, mechanically verifiable coding task")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--model", default="molicoder:latest")
    parser.add_argument("--yes", action="store_true", help="Confirm edits to the current dedicated branch")
    args = parser.parse_args()
    root = args.project.resolve()
    branch, dirty = _git_state(root)
    if not branch:
        parser.error("project must be a Git repository on a named branch")
    if branch in {"main", "master"} or not branch.startswith("molicoder/"):
        parser.error("switch to a dedicated branch named molicoder/<task> before running")
    if dirty:
        parser.error("the dedicated branch must be clean before autonomous edits")
    if not args.yes:
        confirmation = input(f"Allow MoliCoder to edit {root} on branch {branch}? [y/N] ")
        if confirmation.strip().lower() not in {"y", "yes"}:
            return 1
    validator = _validator(root)
    executor = AgentExecutor(
        gateway=InferenceGateway(),
        dispatcher=ToolDispatcher(root, validation=validator.run_and_summarize),
        model=args.model,
    )
    result = executor.run(args.task)
    print(result.final_message or result.error)
    return 0 if result.status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

