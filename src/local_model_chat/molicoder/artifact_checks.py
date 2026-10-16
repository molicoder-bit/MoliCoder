"""Deterministic checks for plan artifacts that Git alone cannot verify."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath


def _target(root: Path, raw: str) -> Path:
    root = Path(root).resolve()
    relative = Path(raw)
    windows = PureWindowsPath(raw)
    if (not raw or not relative.parts or relative.is_absolute() or windows.drive
            or '..' in relative.parts or '..' in windows.parts
            or relative.parts[0].casefold() in {'.git', '.molicoder'}):
        raise ValueError(f"unsafe artifact path: {raw}")
    target = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"artifact path contains a symlink: {raw}")
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"artifact path escapes the project: {raw}")
    return target


def verify_artifact_checks(root: Path, checks: tuple[dict[str, str], ...]) -> tuple[str, ...]:
    """Return precise unmet checks; an empty tuple means every check passed."""
    failures: list[str] = []
    for check in checks:
        raw = check['path']
        try:
            target = _target(root, raw)
            if check['kind'] == 'file_equals':
                if not target.is_file() or target.read_text(encoding='utf-8') != check['content']:
                    failures.append(f"file content differs: {raw}")
            elif check['kind'] == 'directory_empty':
                if not target.is_dir() or any(target.iterdir()):
                    failures.append(f"empty directory missing or contains files: {raw}")
            else:
                failures.append(f"unsupported artifact check: {check['kind']}")
        except (OSError, UnicodeError, ValueError) as exc:
            failures.append(f"artifact check failed for {raw}: {exc}")
    return tuple(failures)


def empty_directory_targets(source: Path, destination: Path, plan) -> tuple[Path, ...]:
    """Preflight approved empty directories before applying a validated run."""
    targets: list[Path] = []
    for task in plan.tasks:
        for check in task.artifact_checks:
            if check['kind'] != 'directory_empty':
                continue
            raw = check['path']
            if verify_artifact_checks(source, (check,)):
                raise ValueError(f"validated empty directory changed: {raw}")
            target = _target(destination, raw)
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise ValueError(f"apply target is not an empty directory: {raw}")
            targets.append(target)
    return tuple(dict.fromkeys(targets))
