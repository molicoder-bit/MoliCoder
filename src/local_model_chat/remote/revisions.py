"""Review a remote candidate against the coordinator's Git objects without applying it."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .artifacts import CandidateManifest, MAX_EXTRACTED_BYTES, _safe_path
from .leases import RemoteLease


class CandidateConflict(ValueError):
    pass


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True,
        timeout=10, check=False,
    )
    if result.returncode != 0:
        raise CandidateConflict("candidate Git revision could not be verified")
    if len(result.stdout) > MAX_EXTRACTED_BYTES + 4096:
        raise CandidateConflict("candidate Git output exceeds size limit")
    return result.stdout


def review_candidate_revision(root: Path, lease: RemoteLease,
                              manifest: CandidateManifest) -> None:
    """Require a clean unchanged base and an exact descendant commit and file set."""
    if (manifest.task_id, manifest.epoch, manifest.base_commit) != (
            lease.task_id, lease.epoch, lease.base_commit):
        raise CandidateConflict("candidate lease or base revision differs")
    root = root.resolve()
    if not (root / ".git").exists():
        raise CandidateConflict("project checkout is not a Git worktree")
    original_head = _git(root, "rev-parse", "HEAD").decode().strip()
    if original_head != lease.base_commit or _git(root, "status", "--porcelain", "-z"):
        raise CandidateConflict("project checkout changed since the remote lease")
    if _git(root, "cat-file", "-t", manifest.candidate_commit).strip() != b"commit":
        raise CandidateConflict("candidate revision is not a Git commit")
    _git(root, "merge-base", "--is-ancestor", lease.base_commit, manifest.candidate_commit)
    changed = _git(root, "diff", "--name-only", "--no-renames", "-z",
                   lease.base_commit, manifest.candidate_commit)
    changed_paths = {part.decode("utf-8") for part in changed.split(b"\0") if part}
    expected_paths = {item.path for item in manifest.files}
    if changed_paths != expected_paths or not all(_safe_path(path) for path in changed_paths):
        raise CandidateConflict("candidate changed paths differ from the manifest")
    for item in manifest.files:
        if int(_git(root, "cat-file", "-s", f"{manifest.candidate_commit}:{item.path}")) != item.size:
            raise CandidateConflict("candidate blob size differs from the manifest")
        data = _git(root, "show", f"{manifest.candidate_commit}:{item.path}")
        if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
            raise CandidateConflict("candidate blob hash differs from the manifest")
    if _git(root, "rev-parse", "HEAD").decode().strip() != original_head:
        raise CandidateConflict("project checkout changed during candidate review")
