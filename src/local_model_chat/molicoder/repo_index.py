from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..attachments import AttachmentError, is_allowed, load_attachment
from .tools import is_sensitive_path


_IGNORED_DIRS = {".git", ".molicoder", ".venv", "node_modules", "build", "dist", "coverage", "__pycache__"}
_SYMBOL = re.compile(r"^\s*(?:async\s+)?(?:def|class|function|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class RepoEntry:
    path: str
    sha256: str
    size: int
    symbols: tuple[str, ...]
    is_test: bool


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    root: Path
    commit: str | None
    index_id: str
    entries: tuple[RepoEntry, ...]
    truncated: bool

    def entry(self, path: str) -> RepoEntry | None:
        return next((item for item in self.entries if item.path == path), None)


def _head(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def build_repository_index(root: str | Path, *, max_files: int = 5000) -> RepositorySnapshot:
    path_root = Path(root).resolve()
    if not path_root.is_dir():
        raise ValueError(f"project folder does not exist: {path_root}")
    entries: list[RepoEntry] = []
    truncated = False
    for directory, dirs, files in os.walk(path_root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(
            name for name in dirs
            if name not in _IGNORED_DIRS and not (parent / name).is_symlink()
        )
        for filename in sorted(files):
            path = parent / filename
            relative = path.relative_to(path_root)
            if not path.is_file() or not path.resolve().is_relative_to(path_root):
                continue
            if is_sensitive_path(relative) or not is_allowed(relative):
                continue
            if len(entries) >= max_files:
                truncated = True
                break
            try:
                attachment = load_attachment(path)
            except AttachmentError:
                continue
            symbols = tuple(dict.fromkeys(_SYMBOL.findall(attachment.text)))[:100]
            entries.append(RepoEntry(
                relative.as_posix(), attachment.sha256, attachment.size, symbols,
                "tests" in relative.parts or path.name.startswith("test_") or path.name.endswith(".test.js"),
            ))
        if truncated:
            break
    commit = _head(path_root)
    digest = hashlib.sha256()
    digest.update((commit or "no-commit").encode("utf-8"))
    for item in entries:
        digest.update(f"\n{item.path}:{item.sha256}".encode("utf-8"))
    return RepositorySnapshot(path_root, commit, digest.hexdigest(), tuple(entries), truncated)
