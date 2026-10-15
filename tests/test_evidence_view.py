from local_model_chat.molicoder.evidence_view import render_task_evidence
from local_model_chat.storage import ChatStore


def test_task_evidence_summarizes_checkpoint_recovery_and_handoff(tmp_path):
    store = ChatStore(tmp_path / "evidence.sqlite3")
    root = tmp_path / "project"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    run_id = store.create_run(project.id, "Make an edit", "HYBRID", provider="codex")
    task_id = store.create_run_task(run_id, "one", "First", "Create output", "low")
    store.update_task_workspace(task_id, worktree=str(root / "worktree"), branch="molicoder/run-1",
                                base_commit="base", changed_paths=("one.txt",))
    store.record_task_completion(
        run_id, task_id, revision="abc123", checks=("project-default",),
        validation_summary="validation passed", output_artifacts=("one.txt",),
        unmet_criteria=(), human_review_status="not_required",
    )
    store.add_event(run_id, "failed_attempt_preserved", {"stash_commit": "stash123"}, task_id)
    store.add_event(run_id, "task_recovery_decision", {"category": "validation", "action": "retry"}, task_id)
    store.add_event(run_id, "provider_takeover", {"provider": "codex", "reason": "local attempt failed"}, task_id)
    result = render_task_evidence(store, run_id)
    assert "Checkpoint: abc123" in result
    assert "Verification: validation passed" in result
    assert "Failed attempt saved: stash123" in result
    assert "Recovery: validation → retry" in result
    assert "Model handoff: codex — local attempt failed" in result
