from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import subprocess
import sys
from pathlib import Path

from .executor import AgentExecutor, ExecutionResult
from ..file_actions import empty_file_intent, create_empty_file
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
    if len(sys.argv) > 1 and sys.argv[1] == 'ask':
        from ..repository_cli import main as repository_main
        return repository_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from ..environment.doctor import doctor_main

        return doctor_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description="Run one bounded MoliCoder executor task.")
    parser.add_argument("task", help="A small, mechanically verifiable coding task")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--model", default="molicoder:latest")
    parser.add_argument("--yes", action="store_true", help="Confirm edits to the current dedicated branch")
    parser.add_argument("--report", type=Path, help="Save executor status, model, usage and tool trace as JSON")
    parser.add_argument("--read-only", action='store_true', help='Expose only read and validation tools; prohibit file edits')
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
    deterministic = empty_file_intent(args.task, root)
    try:
        if deterministic:
            if args.read_only:
                raise ValueError('File creation is unavailable in read-only mode')
            if deterministic.error:
                raise ValueError(deterministic.error)
            summary = create_empty_file(deterministic.target)
            result = ExecutionResult('completed', summary, 0, 0,
                                     trace=(f'Exclusive file creation: {deterministic.target}; verified 0 bytes',))
        else:
            validator = _validator(root)
            executor = AgentExecutor(
                gateway=InferenceGateway(),
                dispatcher=ToolDispatcher(root, validation=validator.run_and_summarize),
                model=args.model,
                read_only=args.read_only,
            )
            result = executor.run(args.task)
    except Exception as exc:  # CLI boundary: preserve an auditable failure instead of a traceback.
        result = ExecutionResult('failed', '', 0, 0, str(exc))
    if args.report:
        args.report.write_text(json.dumps({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'execution_method': 'molicoder-exec CLI (local executor, not desktop Build)',
            'project': str(root), 'branch': branch, 'task': args.task,
            'provider': 'none' if deterministic else 'ollama', 'model': None if deterministic else args.model, 'effort': None,
            'routing_reason': 'Exact empty text-file action needs no inference' if deterministic else
                              'CLI model argument/default; autonomous selection is not implemented here',
            'result': asdict(result),
        }, indent=2), encoding='utf-8')
    print(result.final_message or result.error)
    return 0 if result.status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

