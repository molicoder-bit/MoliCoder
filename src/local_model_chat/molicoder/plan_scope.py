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
        tasks.append(replace(task, expected_paths=tuple(paths)))
    return replace(plan, tasks=tuple(tasks))
