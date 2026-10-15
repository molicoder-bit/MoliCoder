import pytest

from local_model_chat.molicoder.fingerprint import failure_fingerprint
from local_model_chat.molicoder.repair import classify_task_failure


@pytest.mark.parametrize(
    ("status", "error", "validation", "interrupted", "category", "action"),
    [
        ("failed", "quota exceeded", "", False, "provider", "wait_for_reset"),
        ("failed", "login required", "", False, "provider", "wait_for_login"),
        ("timeout", "agent timed out", "", False, "agent_process", "restart_from_checkpoint"),
        ("failed", "", "validation failed\npytest exit 1", False, "validation", "repair_from_diagnostics"),
        ("failed", "architectural invariant conflict: decision #8", "", False, "architecture", "human_review"),
        ("failed", "provider completed without making a repository change", "", False, "no_progress", "stop_repeated_attempt"),
        ("failed", "application closed", "", True, "interrupted", "reconcile_preserved_worktree"),
    ],
)
def test_recovery_categories_are_evidence_based(status, error, validation, interrupted, category, action):
    decision = classify_task_failure(status, error, validation_summary=validation, interrupted=interrupted)
    assert (decision.category, decision.action) == (category, action)
    assert len(decision.fingerprint) == 16


def test_repeated_fingerprint_stops_same_failure_loop():
    error = "src/app.py:37 failed"
    fingerprint = failure_fingerprint(error)
    decision = classify_task_failure("failed", error, fingerprints=(fingerprint, fingerprint))
    assert decision.category == "no_progress"
    assert not decision.retry_allowed
