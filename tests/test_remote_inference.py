import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_model_chat.environment.security import platform_security
from local_model_chat.inference import eligibility
from local_model_chat.inference.budget import BudgetDenied, BudgetManager
from local_model_chat.inference.contracts import InferenceResult
from local_model_chat.inference.onboarding import (
    CustomRoute, CustomRouteFacts, attest_custom_route_facts,
    check_custom_connection, register_custom_route,
)
from local_model_chat.inference.registry import ProviderRegistry
from local_model_chat.remote.inference import RemoteInferenceCoordinator
from local_model_chat.remote.leases import LeaseConflict, RemoteLeaseStore
from local_model_chat.remote.messages import canonical_bytes
from local_model_chat.storage import ChatStore


class Credentials:
    def get(self, _reference):
        return "fixture-key"


class ModelList:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return b'{"data":[{"id":"model-a"}]}'


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def invoke(self, request):
        self.calls.append(request)
        return InferenceResult(request.request_id, "completed", "remote-api",
                               request.model_id, "bounded answer", 4, 2)


def test_remote_paid_request_uses_coordinator_budget_and_never_replays(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(eligibility, "platform_security", lambda: platform_security("Windows"))
    store = ChatStore(tmp_path / "remote-paid.sqlite3")
    project = store.upsert_project("Remote paid", str(tmp_path), "python")
    run_id = store.create_run(project.id, "Read file", "REMOTE_OPT_IN",
                              provider="remote", base_commit="a" * 40)
    task_id = store.create_run_task(run_id, "T1", "Read", "Read one file", "low")
    store.transition_task_state(task_id, "ready", attempts=0,
                                reason="remote fixture ready", actor="system")
    registry = ProviderRegistry(store)
    offering_id = register_custom_route(store, CustomRoute(
        "remote-api", "Remote API", "https://api.example.test/v1", "model-a",
    ))
    attest_custom_route_facts(store, CustomRouteFacts(
        offering_id, "https://api.example.test/pricing", "https://api.example.test/terms",
        "restricted", 8192, 512, 1.0, 2.0, True, True,
    ))
    registry.update_account_secret("manual-key:remote-api", "remote-api", "encrypted-local:" + "a" * 64)
    check_custom_connection(store, offering_id, credentials=Credentials(),
                            opener=lambda *_args, **_kwargs: ModelList())
    registry.set_offering_state(offering_id, status="eligible", actor="user", reason="verified fixture")
    store.record_action_authorization(
        project_id=project.id, action="inference_route", scope=offering_id,
        risk="financial", decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    worker_private = Ed25519PrivateKey.generate()
    worker_public = worker_private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    fingerprint = "f" * 64
    store.record_action_authorization(
        project_id=None, action="remote_pair",
        scope=f"remote_pair:worker-a:{hashlib.sha256(worker_public).hexdigest()}:{fingerprint}",
        risk="mutation", decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    leases = RemoteLeaseStore(store)
    leases.pair_worker("worker-a", base64.b64encode(worker_public).decode(), fingerprint)
    now = datetime.now(timezone.utc)
    lease = leases.offer(task_id, "worker-a", base_commit="a" * 40,
                         worktree_root="remote/fixture", expires_at=now + timedelta(minutes=5), now=now)
    leases.advance(task_id, "worker-a", lease.epoch, 1, "active", now=now)
    envelope = {
        "protocol": 1, "message_id": "message-00000002", "worker_id": "worker-a",
        "task_id": task_id, "epoch": lease.epoch, "sequence": 2, "nonce": "b" * 32,
        "sent_at": now.isoformat(), "coordinator_fingerprint": fingerprint,
        "kind": "request_inference", "body": {"offering_id": offering_id, "prompt": "Summarize one line"},
    }
    signature = base64.b64encode(worker_private.sign(canonical_bytes(envelope))).decode()
    adapter = FakeAdapter()
    coordinator = RemoteInferenceCoordinator(store, adapter=adapter)
    with pytest.raises(BudgetDenied, match="hard budget"):
        coordinator.invoke(envelope, signature)
    assert adapter.calls == []
    BudgetManager(store).set_project_limit(project.id, 0.01, actor="user")
    result = coordinator.invoke(envelope, signature)
    assert result.content == "bounded answer"
    assert len(adapter.calls) == 1
    request = adapter.calls[0]
    assert request.task_id == task_id and request.run_id == run_id
    assert request.sensitivity == "restricted" and request.reservation_id
    assert "fixture-key" not in repr(request)
    with store._connect() as connection:
        reservation = connection.execute(
            "SELECT status, task_scope FROM budget_reservations WHERE id = ?",
            (request.reservation_id,),
        ).fetchone()
    assert tuple(reservation) == ("settled", f"task:{task_id}")
    with pytest.raises(LeaseConflict, match="already dispatched"):
        coordinator.invoke(envelope, signature)
    assert len(adapter.calls) == 1
