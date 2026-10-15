from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from .storage import ChatStore


class ActionRisk(StrEnum):
    READ = "read"
    MUTATION = "mutation"
    PRIVILEGED = "privileged"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"
    FINANCIAL = "financial"


@dataclass(frozen=True, slots=True)
class ActionRequest:
    project_id: int | None
    action: str
    scope: str
    risk: ActionRisk


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    allowed: bool
    reason: str
    record_id: int | None = None


class AuthorizationPolicy:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    def check(self, request: ActionRequest) -> AuthorizationResult:
        if not request.action or not request.scope:
            return AuthorizationResult(False, "Action and scope are required.")
        if request.risk == ActionRisk.READ:
            return AuthorizationResult(True, "Read-only action.")
        record = self.store.latest_action_authorization(
            project_id=request.project_id, action=request.action, scope=request.scope,
        )
        if record is None:
            return AuthorizationResult(False, "This action needs explicit approval.")
        if record["risk"] != request.risk.value:
            return AuthorizationResult(False, "Approval risk does not match the requested action.", record["id"])
        if record["decision"] != "approved":
            return AuthorizationResult(False, f"The latest decision is {record['decision']}.", record["id"])
        try:
            expiry = datetime.fromisoformat(record["expires_at"])
        except ValueError:
            return AuthorizationResult(False, "Approval expiry is invalid.", record["id"])
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            return AuthorizationResult(False, "Approval has expired.", record["id"])
        return AuthorizationResult(True, "Approved for this exact action and scope.", record["id"])

    def require(self, request: ActionRequest) -> int | None:
        result = self.check(request)
        if not result.allowed:
            raise PermissionError(result.reason)
        return result.record_id
