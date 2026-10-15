import subprocess
from dataclasses import replace

import pytest

from local_model_chat.remote.artifacts import stage_candidate
from local_model_chat.remote.leases import RemoteLease
from local_model_chat.remote.revisions import (
    CandidateConflict, import_candidate_bundle, review_candidate_revision,
)
from local_model_chat.remote.worker_checkout import (
    create_candidate_bundle, create_worker_checkout, package_worker_candidate,
)


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def test_worker_checkout_and_candidate_package_are_isolated_and_reviewable(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.test")
    _git(source, "config", "user.name", "Fixture")
    (source / "sample.txt").write_text("base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    lease = RemoteLease(1, "worker-a", 1, "active", base,
                        "remote/task-one", "2099-01-01T00:00:00+00:00", 1)
    worker_root = tmp_path / "worker"
    checkout = create_worker_checkout(source, worker_root, lease)
    assert checkout == worker_root / "remote" / "task-one"
    assert _git(checkout, "rev-parse", "HEAD") == base
    with pytest.raises(ValueError, match="unavailable"):
        create_worker_checkout(source, worker_root, lease)
    (checkout / "sample.txt").write_bytes(b"candidate\n")
    with pytest.raises(ValueError, match="clean"):
        package_worker_candidate(checkout, lease, tmp_path / "dirty.zip")
    _git(checkout, "add", "sample.txt")
    _git(checkout, "commit", "-m", "candidate")
    archive = tmp_path / "candidate.zip"
    manifest = package_worker_candidate(checkout, lease, archive)
    assert manifest.base_commit == base
    staged = stage_candidate(lease, manifest, archive, tmp_path / "staging")
    assert (staged / "sample.txt").read_bytes() == b"candidate\n"
    review_candidate_revision(source, lease, manifest)
    assert _git(source, "rev-parse", "HEAD") == base
    assert (source / "sample.txt").read_text() == "base\n"


def test_worker_package_rejects_deletion_and_escaping_checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.test")
    _git(source, "config", "user.name", "Fixture")
    (source / "sample.txt").write_text("base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    lease = RemoteLease(1, "worker-a", 1, "active", base,
                        "remote/task-one", "2099-01-01T00:00:00+00:00", 1)
    with pytest.raises(ValueError, match="lease path"):
        create_worker_checkout(source, tmp_path / "worker",
                               replace(lease, worktree_root="../escape"))
    with pytest.raises(ValueError, match="not active"):
        create_worker_checkout(source, tmp_path / "worker",
                               replace(lease, state="cancelled"))
    checkout = create_worker_checkout(source, tmp_path / "worker", lease)
    _git(checkout, "rm", "sample.txt")
    _git(checkout, "commit", "-m", "delete")
    with pytest.raises(ValueError, match="deletion"):
        package_worker_candidate(checkout, lease, tmp_path / "deletion.zip")
    assert not (tmp_path / "deletion.zip").exists()


def test_candidate_bundle_imports_objects_without_moving_coordinator_head(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.test")
    _git(source, "config", "user.name", "Fixture")
    (source / "sample.txt").write_text("base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    coordinator = tmp_path / "coordinator"
    subprocess.check_call(["git", "clone", "-q", "--no-hardlinks", str(source), str(coordinator)])
    lease = RemoteLease(1, "worker-a", 1, "active", base,
                        "remote/task-one", "2099-01-01T00:00:00+00:00", 1)
    checkout = create_worker_checkout(source, tmp_path / "worker", lease)
    (checkout / "sample.txt").write_bytes(b"candidate\n")
    _git(checkout, "add", "sample.txt")
    _git(checkout, "commit", "-m", "candidate")
    manifest = package_worker_candidate(checkout, lease, tmp_path / "candidate.zip")
    bundle = create_candidate_bundle(checkout, lease, manifest, tmp_path / "candidate.bundle")
    with pytest.raises(CandidateConflict, match="revision could not"):
        review_candidate_revision(coordinator, lease, manifest)
    import_candidate_bundle(coordinator, bundle, lease, manifest)
    assert _git(coordinator, "rev-parse", "HEAD") == base
    assert (coordinator / "sample.txt").read_text() == "base\n"
    with pytest.raises(CandidateConflict, match="declared candidate"):
        import_candidate_bundle(coordinator, bundle, lease,
                                replace(manifest, candidate_commit="0" * 40))
