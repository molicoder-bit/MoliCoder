"""Coordinator-only inference for an authenticated remote task request.

Provider credentials stay on the coordinator. A duplicate signed message never
causes a second dispatch after the append-only inference ledger has started.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from ..config import data_dir
from ..environment.auth import ProtectedCredentialStore
from ..inference.adapters.openai_compatible import OpenAICompatibleAdapter
from ..inference.api_dispatch import preview_custom_chat
from ..inference.budget import BudgetManager
from ..inference.contracts import InferenceAdapter, InferencePurpose, InferenceRequest, InferenceResult
from ..inference.ledger import InferenceLedger
from ..inference.onboarding import is_api_offering
from ..inference.policy import ControlledGateway, InferencePolicy
from ..inference.registry import ProviderRegistry
from ..storage import ChatStore
from .leases import LeaseConflict
from .messages import RemoteMessageStore


class RemoteInferenceCoordinator:
    def __init__(self, store: ChatStore, *, adapter: InferenceAdapter | None = None) -> None:
        self.store = store
        self.registry = ProviderRegistry(store)
        self.adapter = adapter

    def invoke(self, envelope: dict, signature_b64: str) -> InferenceResult:
        if envelope.get("kind") != "request_inference":
            raise ValueError("signed remote inference request is required")
        body = envelope.get("body")
        if (not isinstance(body, dict) or set(body) != {"offering_id", "prompt"}
                or not isinstance(body["offering_id"], str)
                or not isinstance(body["prompt"], str)):
            raise ValueError("remote inference body is invalid")
        offering_id = body["offering_id"]
        prompt = body["prompt"]
        if not is_api_offering(offering_id):
            raise PermissionError("remote inference supports only controlled API routes")
        RemoteMessageStore(self.store).receive(envelope, signature_b64)
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                """SELECT l.state, l.expires_at, r.id AS run_id, r.project_id
                   FROM remote_leases l JOIN tasks t ON t.id = l.task_id
                   JOIN runs r ON r.id = t.run_id
                   WHERE l.task_id = ? AND l.worker_id = ? AND l.epoch = ?""",
                (envelope["task_id"], envelope["worker_id"], envelope["epoch"]),
            ).fetchone()
        if (row is None or row["state"] != "active"
                or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)):
            raise LeaseConflict("remote task lease is not active")
        preview = preview_custom_chat(self.registry, offering_id, prompt)
        request_id = "remote-" + hashlib.sha256(
            f"{envelope['worker_id']}:{envelope['task_id']}:{envelope['epoch']}:{envelope['message_id']}".encode()
        ).hexdigest()
        with closing(self.store._connect()) as connection:
            already_started = connection.execute(
                "SELECT 1 FROM inference_requests WHERE request_id = ?", (request_id,),
            ).fetchone()
        if already_started:
            raise LeaseConflict("remote inference was already dispatched; reconcile its ledger before retry")
        reservation_id = None
        if preview.worst_case_usd is not None:
            reservation_id = BudgetManager(self.store).reserve(
                request_id, row["project_id"], preview.worst_case_usd,
                task_scope=f"task:{envelope['task_id']}", reuse_active=True,
            )
        request = InferenceRequest(
            request_id, row["project_id"], offering_id, preview.model_id,
            InferencePurpose.EXECUTION, ({"role": "user", "content": prompt},),
            "restricted", (), preview.max_input_tokens, preview.max_output_tokens, 60,
            reservation_id=reservation_id, idempotency_key=envelope["message_id"],
            run_id=row["run_id"], task_id=envelope["task_id"],
        )
        adapter = self.adapter or OpenAICompatibleAdapter(
            self.registry, ProtectedCredentialStore(data_dir() / "credentials"),
        )
        try:
            return ControlledGateway(
                InferencePolicy(self.registry), adapter, InferenceLedger(self.store),
            ).invoke(request)
        except sqlite3.IntegrityError as exc:
            raise LeaseConflict("remote inference was already dispatched; do not retry") from exc
