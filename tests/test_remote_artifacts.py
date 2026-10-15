import hashlib
import zipfile
from dataclasses import replace

import pytest

from local_model_chat.remote.artifacts import ArtifactFile, CandidateManifest, stage_candidate
from local_model_chat.remote.leases import RemoteLease


def _lease():
    return RemoteLease(1, "worker-a", 2, "active", "a" * 40,
                       "remote/checkout", "2026-10-15T21:00:00+00:00", 3)


def test_remote_candidate_is_staged_only_after_exact_manifest_validation(tmp_path) -> None:
    payload = b"print('safe')\n"
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as contents:
        contents.writestr("src/safe.py", payload)
    manifest = CandidateManifest(1, 2, "a" * 40, "b" * 40, (
        ArtifactFile("src/safe.py", len(payload), hashlib.sha256(payload).hexdigest()),
    ))
    staged = stage_candidate(_lease(), manifest, archive, tmp_path / "staging")
    assert (staged / "src" / "safe.py").read_bytes() == payload
    assert not (tmp_path / "src" / "safe.py").exists()
    with pytest.raises(ValueError, match="revision"):
        stage_candidate(_lease(), replace(manifest, epoch=1), archive, tmp_path / "staging")


@pytest.mark.parametrize("name", ["../escape", "/absolute", "src\\bad.py", ".git/config", "src//bad.py"])
def test_remote_candidate_rejects_unsafe_paths(tmp_path, name) -> None:
    payload = b"x"
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as contents:
        contents.writestr(name, payload)
    manifest = CandidateManifest(1, 2, "a" * 40, "b" * 40, (
        ArtifactFile(name, 1, hashlib.sha256(payload).hexdigest()),
    ))
    with pytest.raises(ValueError, match="unsafe"):
        stage_candidate(_lease(), manifest, archive, tmp_path / "staging")
    assert not list((tmp_path / "staging").glob("candidate-*"))


def test_remote_candidate_rejects_hash_mismatch_and_symlink(tmp_path) -> None:
    archive = tmp_path / "bad-hash.zip"
    with zipfile.ZipFile(archive, "w") as contents:
        contents.writestr("safe.py", b"different")
    manifest = CandidateManifest(1, 2, "a" * 40, "b" * 40, (
        ArtifactFile("safe.py", 9, hashlib.sha256(b"expected!").hexdigest()),
    ))
    with pytest.raises(ValueError, match="hash differs"):
        stage_candidate(_lease(), manifest, archive, tmp_path / "staging")
    assert not list((tmp_path / "staging").glob("candidate-*"))
    link = zipfile.ZipInfo("safe.py")
    link.create_system = 3
    link.external_attr = (0o120777 << 16)
    with zipfile.ZipFile(archive, "w") as contents:
        contents.writestr(link, b"target")
    with pytest.raises(ValueError, match="unsafe"):
        stage_candidate(_lease(), replace(manifest, files=(
            ArtifactFile("safe.py", 6, hashlib.sha256(b"target").hexdigest()),
        )), archive, tmp_path / "staging")
