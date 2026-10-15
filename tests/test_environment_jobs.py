from datetime import datetime, timedelta, timezone

from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.install import InstallReceipt
from local_model_chat.environment.jobs import InstallJobQueue
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.environment.policy import plan_setup
from local_model_chat.storage import ChatStore


def _plans(store):
    inventory = EnvironmentReport(
        1, "Windows", "x86_64", 8, 16 * 1024**3, 100 * 1024**3,
        "unknown", "fixture",
        (DiscoveredTool("ollama", None, None, "missing"),
         DiscoveredTool("kandev", None, None, "missing")), (),
    )
    manifests = [item for item in load_manifests()
                 if item.system == "Windows" and item.tool_id in {"ollama", "kandev"}]
    return [plan_setup(store, inventory, item) for item in manifests]


def _approve(store, plan, *, expires=None):
    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="approved", actor="user",
        expires_at=(expires or datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )


def test_waiting_login_does_not_block_second_approved_job_and_restart_retains_state(tmp_path) -> None:
    db = tmp_path / "jobs.sqlite3"
    store = ChatStore(db)
    plans = _plans(store)
    for plan in plans:
        _approve(store, plan)
    refreshed = _plans(store)
    queue = InstallJobQueue(store)
    first_id, second_id = [queue.enqueue(plan) for plan in refreshed]
    assert queue.enqueue(refreshed[0]) == first_id

    first = queue.claim_next()
    assert first.id == first_id
    queue.transition(first.id, "waiting_for_login")
    second = queue.claim_next()
    assert second.id == second_id
    receipt = InstallReceipt(second.tool_id, second.version, second.manifest_sha256,
                             str(tmp_path / "managed"), str(tmp_path / "managed" / "tool"),
                             datetime.now(timezone.utc).isoformat())
    queue.transition(second.id, "completed", receipt=receipt)

    reopened = InstallJobQueue(ChatStore(db)).list_jobs()
    assert [job.status for job in reopened] == ["waiting_for_login", "completed"]
    assert reopened[1].receipt_json


def test_expired_approval_is_rechecked_before_claim(tmp_path) -> None:
    store = ChatStore(tmp_path / "jobs.sqlite3")
    plan = _plans(store)[0]
    _approve(store, plan)
    queue = InstallJobQueue(store)
    job_id = queue.enqueue(_plans(store)[0])
    store.record_action_authorization(
        project_id=None, action="setup", scope=plan.scope, risk="mutation",
        decision="revoked", actor="user",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    assert queue.claim_next() is None
    assert queue.list_jobs()[0].status == "waiting_for_authorization"
    _approve(store, plan)
    assert queue.refresh_waiting_authorizations() == (job_id,)
    assert queue.claim_next().id == job_id


def test_interrupted_job_requires_review_after_restart(tmp_path) -> None:
    db = tmp_path / "jobs.sqlite3"
    store = ChatStore(db)
    plan = _plans(store)[0]
    _approve(store, plan)
    queue = InstallJobQueue(store)
    job_id = queue.enqueue(_plans(store)[0])
    assert queue.claim_next().id == job_id
    assert InstallJobQueue(ChatStore(db)).recover_interrupted() == (job_id,)
    assert InstallJobQueue(ChatStore(db)).list_jobs()[0].status == "waiting_for_permission"
