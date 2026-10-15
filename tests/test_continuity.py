from local_model_chat.controller import MainController
from local_model_chat.knowledge import ProjectKnowledge
from local_model_chat.storage import ChatStore


def test_continuity_after_restart_uses_approved_knowledge_and_current_task_state(tmp_path) -> None:
    db = tmp_path / "continuity.sqlite3"
    store = ChatStore(db)
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    knowledge = ProjectKnowledge(store)
    objective = knowledge.propose_item(
        project.id, kind="objective", title="Primary goal", body="Build a local assistant",
        author="user", source_ref="user-approved-brief", source_revision="brief-v1",
    )
    knowledge.verify_item(objective.id, actor="user")
    decision = knowledge.propose_decision(
        project.id, title="Use SQLite", body="Store project history locally",
        rationale="Works offline", author="user", source_ref="architecture.md",
        source_revision="commit-abc",
    )
    knowledge.approve_decision(decision.id, actor="user")
    completed_run = store.create_run(project.id, "Finish foundation", "LOCAL_ONLY")
    completed_task = store.create_run_task(completed_run, "T1", "Build storage", "Store", "low")
    store.update_run_task(completed_task, "completed", 1)
    store.finish_run(completed_run, "completed")
    current_run = store.create_run(project.id, "Add retrieval", "LOCAL_ONLY")
    blocked_task = store.create_run_task(current_run, "T2", "Index files", "Index", "medium")
    store.update_run_task(blocked_task, "blocked", 1)
    store.update_run_state(current_run, "needs_attention", recovery_reason="missing dependency")

    answer = MainController(ChatStore(db)).handle("Where did we leave off?", project.id)

    assert answer is not None and answer.kind == "continuity"
    assert "Build a local assistant" in answer.content
    assert f"#{decision.id}" in answer.content and "Works offline" in answer.content
    assert f"run #{completed_run} task T1" in answer.content
    assert f"run #{current_run} task T2" in answer.content
    assert "missing dependency" in answer.content
    assert "brief-v1" in answer.content and "commit-abc" in answer.content


def test_continuity_marks_missing_evidence_instead_of_inventing_progress(tmp_path) -> None:
    store = ChatStore(tmp_path / "empty.sqlite3")
    project = store.upsert_project("Empty", str(tmp_path / "empty"), "python")

    answer = MainController(store).handle("Where did we leave off?", project.id)

    assert answer is not None
    assert "No verified project objective" in answer.content
    assert "No verified record yet" in answer.content
    assert "Confirm and record" in answer.content
