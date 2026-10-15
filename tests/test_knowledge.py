import sqlite3

import pytest

from local_model_chat.domain import Message
from local_model_chat.knowledge import ProjectKnowledge
from local_model_chat.storage import ChatStore


def test_decision_correction_keeps_previous_approved_revision_until_reapproved(tmp_path) -> None:
    db = tmp_path / "knowledge.sqlite3"
    store = ChatStore(db)
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    knowledge = ProjectKnowledge(store)
    proposed = knowledge.propose_decision(
        project.id, title="Use SQLite", body="Persist locally", rationale="Offline access",
        author="user", source_ref="architecture.md", source_revision="abc",
    )
    assert proposed.status == "proposed"
    assert knowledge.approved_decisions(project.id) == []

    approved = knowledge.approve_decision(proposed.id, actor="user")
    assert approved.status == "approved"
    corrected = knowledge.correct_decision(
        approved.id, title="Use SQLite with WAL", body="Persist locally with WAL",
        rationale="Concurrent readers", author="user", source_ref="architecture.md",
        source_revision="def",
    )

    assert corrected.revision == 2
    assert corrected.status == "proposed"
    old = knowledge.approved_revision(approved.id)
    assert old is not None and old.revision == 1 and old.body == "Persist locally"
    assert knowledge.approved_decisions(project.id)[0].body == "Persist locally"
    latest = ProjectKnowledge(ChatStore(db)).approve_decision(approved.id, actor="user")
    assert latest.revision == 2 and latest.status == "approved"
    assert ProjectKnowledge(ChatStore(db)).approved_decisions(project.id)[0].body == "Persist locally with WAL"
    with sqlite3.connect(db) as connection:
        revisions = connection.execute(
            "SELECT revision FROM knowledge_revisions WHERE item_id = ? ORDER BY revision", (approved.id,)
        ).fetchall()
    assert revisions == [(1,), (2,)]


def test_supersession_invalidation_and_archival_preserve_history(tmp_path) -> None:
    store = ChatStore(tmp_path / "knowledge.sqlite3")
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    knowledge = ProjectKnowledge(store)
    old = knowledge.propose_decision(
        project.id, title="Option A", body="Choose A", rationale="Initial plan", author="user",
    )
    knowledge.approve_decision(old.id, actor="user")
    replacement = knowledge.propose_decision(
        project.id, title="Option B", body="Choose B", rationale="New evidence",
        author="user", supersedes_item_id=old.id,
    )
    assert knowledge._get(old.id).status == "approved"
    knowledge.approve_decision(replacement.id, actor="user")
    assert knowledge._get(old.id).status == "superseded"
    assert [item.id for item in knowledge.approved_decisions(project.id)] == [replacement.id]
    knowledge.invalidate(replacement.id, actor="user")
    assert knowledge.approved_decisions(project.id) == []
    assert knowledge.approved_revision(replacement.id) is not None
    other = knowledge.propose_decision(
        project.id, title="Option C", body="Choose C", rationale="Another proposal", author="user",
    )
    knowledge.archive(other.id, actor="user")
    assert knowledge._get(other.id).status == "archived"


def test_model_cannot_promote_or_correct_approved_decision(tmp_path) -> None:
    store = ChatStore(tmp_path / "knowledge.sqlite3")
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    knowledge = ProjectKnowledge(store)
    note = knowledge.propose_decision(
        project.id, title="Model note", body="Unverified", rationale="Model suggestion", author="model",
    )
    with pytest.raises(PermissionError):
        knowledge.approve_decision(note.id, actor="model")
    approved = knowledge.approve_decision(note.id, actor="user")
    with pytest.raises(PermissionError):
        knowledge.correct_decision(
            approved.id, title="Overwrite", body="False", rationale="None", author="model",
        )
    assert knowledge._get(approved.id).body == "Unverified"


def test_conversation_and_run_links_stay_in_one_project_after_restart(tmp_path) -> None:
    db = tmp_path / "linked.sqlite3"
    store = ChatStore(db)
    first = store.upsert_project("First", str(tmp_path / "first"), "python")
    second = store.upsert_project("Second", str(tmp_path / "second"), "python")
    conversation = store.create_conversation("local-test")
    store.link_conversation_project(conversation.id, first.id)
    run_id = store.create_run(first.id, "Work", "LOCAL_ONLY")
    decision = ProjectKnowledge(store).propose_decision(
        first.id, title="Choice", body="Use local", rationale="Privacy", author="user",
    )
    ProjectKnowledge(store).approve_decision(decision.id, actor="user")

    conversation_link = store.link_knowledge(decision.id, relationship="discussed", conversation_id=conversation.id)
    run_link = store.link_knowledge(decision.id, relationship="constraint", run_id=run_id)

    reopened = ChatStore(db)
    assert reopened.get_conversation_project_id(conversation.id) == first.id
    assert reopened.list_knowledge_links(conversation_id=conversation.id)[0]["id"] == conversation_link
    assert reopened.list_knowledge_links(run_id=run_id)[0]["id"] == run_link
    assert reopened.link_knowledge(decision.id, relationship="constraint", run_id=run_id) == run_link
    other_run = reopened.create_run(second.id, "Other", "LOCAL_ONLY")
    with pytest.raises(ValueError):
        reopened.link_knowledge(decision.id, relationship="constraint", run_id=other_run)
    reopened.add_message(Message(None, conversation.id, "user", "hi"))
    with pytest.raises(ValueError):
        reopened.link_conversation_project(conversation.id, second.id)
