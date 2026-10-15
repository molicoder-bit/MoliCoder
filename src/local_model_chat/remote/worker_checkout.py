"""Prepare a worker-owned Git checkout and a bounded candidate package."""

from __future__ import annotations

import hashlib
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .artifacts import (
    MAX_ARCHIVE_BYTES, MAX_EXTRACTED_BYTES, MAX_FILES, ArtifactFile,
    CandidateManifest, _safe_path,
)
from .leases import RemoteLease


MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            timeout=15, check=False)
    if result.returncode != 0:
        raise ValueError("worker Git revision or checkout is invalid")
    if len(result.stdout) > MAX_EXTRACTED_BYTES:
        raise ValueError("worker Git output exceeds size limit")
    return result.stdout


def create_worker_checkout(source_repo: Path, worker_root: Path,
                           lease: RemoteLease) -> Path:
    """Create only the leased detached checkout under a worker-owned root."""
    relative = PurePosixPath(lease.worktree_root)
    if (not lease.worktree_root or relative.is_absolute()
            or str(relative) != lease.worktree_root
            or "\\" in lease.worktree_root or ":" in lease.worktree_root
            or any(part in {"", ".", "..", ".git"} for part in relative.parts)
            or not re.fullmatch(r"[0-9a-f]{40,64}", lease.base_commit)):
        raise ValueError("worker checkout lease path or base is invalid")
    if (lease.state not in {"offered", "active"}
            or datetime.fromisoformat(lease.expires_at) <= datetime.now(timezone.utc)):
        raise ValueError("worker checkout lease is not active")
    source = source_repo.resolve(strict=True)
    if _git(source, "cat-file", "-t", lease.base_commit).strip() != b"commit":
        raise ValueError("worker does not have the leased base commit")
    root = worker_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / Path(*relative.parts)).resolve()
    if not destination.is_relative_to(root) or destination.exists():
        raise ValueError("worker checkout destination is unavailable")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _git(source, "worktree", "add", "--detach", str(destination), lease.base_commit)
    if _git(destination, "rev-parse", "HEAD").decode().strip() != lease.base_commit:
        raise ValueError("worker checkout does not match the leased base")
    return destination


def package_worker_candidate(checkout: Path, lease: RemoteLease,
                             archive: Path) -> CandidateManifest:
    """Package committed regular-file changes only; deletions and links fail closed."""
    root = checkout.resolve(strict=True)
    relative = Path(*PurePosixPath(lease.worktree_root).parts)
    if (lease.state != "active" or datetime.fromisoformat(lease.expires_at) <= datetime.now(timezone.utc)
            or tuple(root.parts[-len(relative.parts):]) != relative.parts):
        raise ValueError("worker candidate is outside its active leased checkout")
    if _git(root, "status", "--porcelain", "-z"):
        raise ValueError("worker candidate checkout must be clean")
    candidate = _git(root, "rev-parse", "HEAD").decode().strip()
    if candidate == lease.base_commit:
        raise ValueError("worker candidate has no new commit")
    _git(root, "merge-base", "--is-ancestor", lease.base_commit, candidate)
    paths = [part.decode("utf-8") for part in _git(
        root, "diff", "--name-only", "--no-renames", "-z", lease.base_commit, candidate,
    ).split(b"\0") if part]
    if not paths or len(paths) > MAX_FILES or len({path.casefold() for path in paths}) != len(paths):
        raise ValueError("worker candidate file count or names are invalid")
    files: list[ArtifactFile] = []
    total = 0
    for path in paths:
        if not _safe_path(path):
            raise ValueError("worker candidate contains an unsafe path")
        tree_entry = _git(root, "ls-tree", candidate, "--", path).decode().split(maxsplit=1)
        mode = tree_entry[0] if tree_entry else ""
        if mode not in {"100644", "100755"}:
            raise ValueError("worker candidate contains a deletion or non-regular file")
        size = int(_git(root, "cat-file", "-s", f"{candidate}:{path}"))
        total += size
        if total > MAX_EXTRACTED_BYTES:
            raise ValueError("worker candidate exceeds extracted size limit")
        data = _git(root, "show", f"{candidate}:{path}")
        if len(data) != size:
            raise ValueError("worker candidate blob changed during packaging")
        files.append(ArtifactFile(path, size, hashlib.sha256(data).hexdigest()))
    if archive.exists():
        raise ValueError("worker candidate archive already exists")
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as contents:
            for item in files:
                contents.writestr(item.path, _git(root, "show", f"{candidate}:{item.path}"))
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("worker candidate archive exceeds size limit")
        if _git(root, "rev-parse", "HEAD").decode().strip() != candidate:
            raise ValueError("worker candidate changed during packaging")
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    return CandidateManifest(lease.task_id, lease.epoch, lease.base_commit,
                             candidate, tuple(files))


def create_candidate_bundle(checkout: Path, lease: RemoteLease,
                            manifest: CandidateManifest, bundle: Path) -> Path:
    """Export only the candidate's new Git objects above the leased base."""
    root = checkout.resolve(strict=True)
    if (lease.state != "active" or manifest.task_id != lease.task_id
            or manifest.epoch != lease.epoch or manifest.base_commit != lease.base_commit
            or _git(root, "rev-parse", "HEAD").decode().strip() != manifest.candidate_commit
            or _git(root, "status", "--porcelain", "-z")):
        raise ValueError("worker bundle no longer matches a clean active candidate")
    _git(root, "merge-base", "--is-ancestor", lease.base_commit, manifest.candidate_commit)
    if bundle.exists():
        raise ValueError("worker candidate bundle already exists")
    bundle.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git(root, "bundle", "create", str(bundle), "HEAD", f"^{lease.base_commit}")
        if bundle.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("worker candidate bundle exceeds size limit")
    except Exception:
        bundle.unlink(missing_ok=True)
        raise
    return bundle
