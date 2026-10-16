"""Exact, revision-bound authorization for a reviewed Build plan."""

from __future__ import annotations

import hashlib
import json

from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..storage import ChatStore


def plan_digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def approval_scope(run: dict, plan_id: int, payload: dict) -> str:
    return ":".join((
        "build-plan", str(run["project_id"]), str(run["id"]), str(plan_id),
        plan_digest(payload), str(run["base_commit"]), str(run["mode"]),
        str(run["provider"]), str(run["model"]),
    ))


def require_approved_plan(store: ChatStore, run_id: int) -> tuple[dict, dict]:
    run = store.get_run(run_id)
    plan = store.get_latest_plan_record(run_id)
    if run is None or plan is None:
        raise PermissionError("Build plan or run is missing")
    AuthorizationPolicy(store).require(ActionRequest(
        int(run["project_id"]), "build_plan_execute",
        approval_scope(run, int(plan["id"]), plan["payload"]), ActionRisk.MUTATION,
    ))
    return run, plan
