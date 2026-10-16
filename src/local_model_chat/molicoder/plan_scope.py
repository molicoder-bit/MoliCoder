"""Validate planned paths before allocating an execution workspace."""
from dataclasses import replace
from pathlib import Path, PureWindowsPath


def validate_plan_scope(plan, root):
    root = Path(root).resolve()
    tasks = []
    for task in plan.tasks:
        paths = []
        for raw in task.expected_paths:
            path = Path(raw)
            windows = PureWindowsPath(raw)
            if not raw.strip() or '..' in windows.parts or '..' in path.parts:
                raise ValueError('Planned path escapes the project. Choose a target inside the project or use an explicit local folder action.')
            if windows.drive and not path.is_absolute():
                raise ValueError('Planned absolute path is outside the project scope.')
            resolved = (root / path).resolve()
            if not resolved.is_relative_to(root):
                raise ValueError('Planned path is outside the selected project. Use an explicit local folder action for other locations.')
            paths.append(resolved.relative_to(root).as_posix())
        checks = []
        for check in task.artifact_checks:
            raw = check["path"]
            path = Path(raw)
            windows = PureWindowsPath(raw)
            if ('..' in windows.parts or '..' in path.parts or windows.drive
                    or path.is_absolute() or not raw.strip()):
                raise ValueError('Artifact check path escapes the project.')
            resolved = (root / path).resolve()
            if not resolved.is_relative_to(root) or resolved == root:
                raise ValueError('Artifact check path is outside the selected project.')
            relative = resolved.relative_to(root).as_posix()
            if relative not in paths or relative.split('/')[0] in {'.git', '.molicoder'}:
                raise ValueError('Artifact check path must be in the approved task scope.')
            checks.append(dict(check) | {'path': relative})
        tasks.append(replace(task, expected_paths=tuple(paths), artifact_checks=tuple(checks)))
    return replace(plan, tasks=tuple(tasks))
