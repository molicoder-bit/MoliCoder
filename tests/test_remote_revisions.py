import hashlib
import subprocess
from dataclasses import replace

import pytest

from local_model_chat.remote.artifacts import ArtifactFile, CandidateManifest
from local_model_chat.remote.leases import RemoteLease
from local_model_chat.remote.revisions import CandidateConflict, review_candidate_revision


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _fixture(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.test")
    _git(root, "config", "user.name", "Fixture")
    (root / "sample.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "sample.txt")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    data = b"candidate\n"
    (root / "sample.txt").write_bytes(data)
    _git(root, "add", "sample.txt")
    _git(root, "commit", "-m", "candidate")
    candidate = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "--hard", base)
    lease = RemoteLease(1, "worker-a", 1, "active", base,
                        "remote/checkout", "2026-10-15T21:00:00+00:00", 2)
    manifest = CandidateManifest(1, 1, base, candidate, (
        ArtifactFile("sample.txt", len(data), hashlib.sha256(data).hexdigest()),
    ))
    return root, lease, manifest


def test_remote_candidate_git_revision_matches_clean_base_and_manifest(tmp_path) -> None:
    root, lease, manifest = _fixture(tmp_path)
    review_candidate_revision(root, lease, manifest)
    assert _git(root, "rev-parse", "HEAD") == lease.base_commit
    with pytest.raises(CandidateConflict, match="hash differs"):
        review_candidate_revision(root, lease, replace(manifest, files=(
            replace(manifest.files[0], sha256="0" * 64),
        )))
    with pytest.raises(CandidateConflict, match="paths differ"):
        review_candidate_revision(root, lease, replace(manifest, files=(
            replace(manifest.files[0], path="other.txt"),
        )))
    (root / "untracked.txt").write_text("local work", encoding="utf-8")
    with pytest.raises(CandidateConflict, match="checkout changed"):
        review_candidate_revision(root, lease, manifest)
