import base64
import hashlib
import ipaddress
import threading
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID

from local_model_chat.remote.leases import LeaseConflict, RemoteLeaseStore
from local_model_chat.remote.transport import create_loopback_server, post_signed_message
from local_model_chat.storage import ChatStore


def _certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MoliCoder loopback test")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MoliCoder loopback test")]))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]), critical=False)
            .sign(key, hashes.SHA256()))
    certificate = tmp_path / "coordinator.pem"
    private_key = tmp_path / "coordinator-key.pem"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return certificate, private_key, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def test_loopback_tls_accepts_exact_signed_message_and_rejects_pin_mismatch(tmp_path) -> None:
    certificate, tls_key, fingerprint = _certificate(tmp_path)
    store = ChatStore(tmp_path / "transport.sqlite3")
    project = store.upsert_project("Remote TLS", str(tmp_path), "python")
    run_id = store.create_run(project.id, "Read file", "REMOTE_OPT_IN",
                              provider="remote", base_commit="a" * 40)
    task_id = store.create_run_task(run_id, "T1", "Read", "Read a line", "low")
    store.transition_task_state(task_id, "ready", attempts=0,
                                reason="remote fixture ready", actor="system")
    worker_key = ed25519.Ed25519PrivateKey.generate()
    public = worker_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    store.record_action_authorization(
        project_id=None, action="remote_pair",
        scope=f"remote_pair:worker-a:{hashlib.sha256(public).hexdigest()}:{fingerprint}",
        risk="mutation", decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    leases = RemoteLeaseStore(store)
    leases.pair_worker("worker-a", base64.b64encode(public).decode(), fingerprint)
    now = datetime.now(timezone.utc)
    lease = leases.offer(task_id, "worker-a", base_commit="a" * 40,
                         worktree_root="remote/fixture", expires_at=now + timedelta(minutes=3), now=now)
    envelope = {
        "protocol": 1, "message_id": "message-00000001", "worker_id": "worker-a",
        "task_id": task_id, "epoch": lease.epoch, "sequence": 1, "nonce": "b" * 32,
        "sent_at": now.isoformat(), "coordinator_fingerprint": fingerprint,
        "kind": "lease_accept", "body": {},
    }
    server = create_loopback_server(store, certificate, tls_key)
    assert server.server_address[0] == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with pytest.raises(LeaseConflict, match="pin differs"):
            post_signed_message(port, envelope, worker_key, certificate, "0" * 64)
        assert leases.get(task_id).last_sequence == 0
        receipt = post_signed_message(port, envelope, worker_key, certificate, fingerprint)
        assert receipt["message_id"] == envelope["message_id"]
        assert post_signed_message(port, envelope, worker_key, certificate, fingerprint) == receipt
        assert leases.get(task_id).state == "active"
        with pytest.raises(LeaseConflict, match="rejected"):
            post_signed_message(port, {**envelope, "message_id": "message-00000002", "sequence": 2,
                                       "nonce": "c" * 32}, ed25519.Ed25519PrivateKey.generate(),
                                certificate, fingerprint)
        leases.request_cancel(task_id, reason="Stop the remote fixture")
        heartbeat = {**envelope, "message_id": "message-00000003", "sequence": 2,
                     "nonce": "d" * 32, "kind": "heartbeat"}
        assert post_signed_message(port, heartbeat, worker_key, certificate, fingerprint)[
            "cancel_requested"] is True
        leases.revoke_worker("worker-a")
        with pytest.raises(LeaseConflict, match="rejected"):
            post_signed_message(port, envelope, worker_key, certificate, fingerprint)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
