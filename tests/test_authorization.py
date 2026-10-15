from datetime import datetime, timedelta, timezone

import pytest

from local_model_chat.authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from local_model_chat.storage import ChatStore


@pytest.mark.parametrize(
    "risk", [ActionRisk.MUTATION, ActionRisk.PRIVILEGED, ActionRisk.DESTRUCTIVE, ActionRisk.EXTERNAL, ActionRisk.FINANCIAL],
)
def test_high_risk_actions_require_matching_persisted_approval(tmp_path, risk: ActionRisk) -> None:
    db = tmp_path / "auth.sqlite3"
    store = ChatStore(db)
    project = store.upsert_project("Example", str(tmp_path / "example"), "python")
    request = ActionRequest(project.id, "perform", "exact-target", risk)
    policy = AuthorizationPolicy(store)

    assert not policy.check(request).allowed
    with pytest.raises(PermissionError):
        policy.require(request)
    record_id = store.record_action_authorization(
        project_id=project.id, action=request.action, scope=request.scope, risk=risk.value,
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )

    assert AuthorizationPolicy(ChatStore(db)).require(request) == record_id
    assert not policy.check(ActionRequest(project.id, "perform", "other-target", risk)).allowed
    assert not policy.check(ActionRequest(None, "perform", "exact-target", risk)).allowed


def test_latest_denial_or_expiry_blocks_previous_approval(tmp_path) -> None:
    store = ChatStore(tmp_path / "auth.sqlite3")
    request = ActionRequest(None, "install", "tool@1.0", ActionRisk.PRIVILEGED)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.record_action_authorization(
        project_id=None, action="install", scope="tool@1.0", risk="privileged",
        decision="approved", actor="user", expires_at=future,
    )
    policy = AuthorizationPolicy(store)
    assert policy.check(request).allowed
    store.record_action_authorization(
        project_id=None, action="install", scope="tool@1.0", risk="privileged",
        decision="revoked", actor="user", expires_at=future,
    )
    assert not policy.check(request).allowed
    store.record_action_authorization(
        project_id=None, action="install", scope="tool@1.0", risk="privileged",
        decision="approved", actor="user",
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert not policy.check(request).allowed


def test_read_only_action_needs_no_grant(tmp_path) -> None:
    policy = AuthorizationPolicy(ChatStore(tmp_path / "auth.sqlite3"))
    assert policy.check(ActionRequest(None, "status", "selected-project", ActionRisk.READ)).allowed
