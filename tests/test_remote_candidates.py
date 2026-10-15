import base64
import hashlib
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_model_chat.remote.candidates import RemoteCandidateCoordinator
from local_model_chat.remote.leases import LeaseConflict, RemoteLeaseStore
from local_model_chat.remote.messages import RemoteMessageStore, canonical_bytes
from local_model_chat.storage import ChatStore


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _fixture(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.test")
    _git(root, "config", "user.name", "Fixture")
    (root / "sample.txt").write_text("base\n")
    _git(root, "add", "sample.txt")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    payload = b"candidate\n"
    (root / "sample.txt").write_bytes(payload)
    _git(root, "add", "sample.txt")
    _git(root, "commit", "-m", "candidate")
    candidate = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "--hard", base)
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as contents:
        contents.writestr("sample.txt", payload)
    store = ChatStore(tmp_path / "remote.sqlite3")
    project = store.upsert_project("Remote fixture", str(root), "python")
    run_id = store.create_run(project.id, "Review remote candidate", "REMOTE_OPT_IN",
                              provider="remote", base_commit=base)
    task_id = store.create_run_task(run_id, "T1", "Edit", "Change sample", "low")
    store.transition_task_state(task_id, "ready", attempts=0,
                                reason="ready for remote", actor="system")
    private = Ed25519PrivateKey.generate()
    key = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    fingerprint = "f" * 64
    store.record_action_authorization(
        project_id=None, action="remote_pair",
        scope=f"remote_pair:worker-a:{hashlib.sha256(key).hexdigest()}:{fingerprint}",
        risk="mutation", decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    leases = RemoteLeaseStore(store)
    leases.pair_worker("worker-a", base64.b64encode(key).decode(), fingerprint)
    now = datetime.now(timezone.utc)
    lease = leases.offer(task_id, "worker-a", base_commit=base,
                         worktree_root="remote/fixture", expires_at=now + timedelta(minutes=5))
    accept = {
        "protocol": 1, "message_id": "message-00000001", "worker_id": "worker-a",
        "task_id": task_id, "epoch": lease.epoch, "sequence": 1, "nonce": "a" * 32,
        "sent_at": now.isoformat(), "coordinator_fingerprint": fingerprint,
        "kind": "lease_accept", "body": {},
    }
    RemoteMessageStore(store).receive(
        accept, base64.b64encode(private.sign(canonical_bytes(accept))).decode(),
    )
    manifest = {
        **accept, "message_id": "message-00000002", "sequence": 2,
        "nonce": "b" * 32, "kind": "artifact_manifest",
        "body": {"base_commit": base, "candidate_commit": candidate,
                 "files": [{"path": "sample.txt", "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest()}]},
    }
    return store, private, root, archive, manifest


def _signature(private, manifest):
    return base64.b64encode(private.sign(canonical_bytes(manifest))).decode()


def test_signed_candidate_review_stages_without_applying(tmp_path):
    store, private, root, archive, manifest = _fixture(tmp_path)
    review = RemoteCandidateCoordinator(store).review(
        manifest, _signature(private, manifest), archive, root, tmp_path / "staging",
    )
    assert review.candidate_commit == manifest["body"]["candidate_commit"]
    assert (review.staged_path / "sample.txt").read_bytes() == b"candidate\n"
    assert (root / "sample.txt").read_text() == "base\n"
    assert _git(root, "rev-parse", "HEAD") == manifest["body"]["base_commit"]
    assert any(event.kind == "remote_candidate_reviewed" for event in store.list_events(1))
    repeated = RemoteCandidateCoordinator(store).review(
        manifest, _signature(private, manifest), archive, root, tmp_path / "staging",
    )
    assert repeated == review
    assert len(list((tmp_path / "staging").glob("candidate-*"))) == 1


def test_signed_candidate_review_rejects_tamper_and_changed_checkout(tmp_path):
    store, private, root, archive, manifest = _fixture(tmp_path)
    tampered = {**manifest, "body": {**manifest["body"], "candidate_commit": "0" * 40}}
    with pytest.raises(LeaseConflict, match="signature"):
        RemoteCandidateCoordinator(store).review(
            tampered, _signature(private, manifest), archive, root, tmp_path / "staging",
        )
    (root / "untracked.txt").write_text("local work")
    with pytest.raises(ValueError, match="checkout changed"):
        RemoteCandidateCoordinator(store).review(
            manifest, _signature(private, manifest), archive, root, tmp_path / "staging",
        )
    assert not list((tmp_path / "staging").glob("candidate-*"))
    assert not any(event.kind == "remote_candidate_reviewed" for event in store.list_events(1))
    assert any(event.kind == "remote_candidate_conflict" for event in store.list_events(1))


def test_signed_candidate_review_requires_selected_project(tmp_path):
    store, private, root, archive, manifest = _fixture(tmp_path)
    with pytest.raises(LeaseConflict, match="selected remote project"):
        RemoteCandidateCoordinator(store).review(
            manifest, _signature(private, manifest), archive, tmp_path,
            tmp_path / "staging",
        )
