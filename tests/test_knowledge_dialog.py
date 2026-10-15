from local_model_chat.domain import Message
from local_model_chat.knowledge import ProjectKnowledge
from local_model_chat.storage import ChatStore
from local_model_chat.ui.knowledge_dialog import KnowledgeReviewDialog


def test_review_and_correct_decision_preserves_transcript_and_old_revision(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "review.sqlite3")
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    conversation = store.create_conversation("local-test")
    store.add_message(Message(None, conversation.id, "user", "Original conversation"))
    knowledge = ProjectKnowledge(store)
    decision = knowledge.propose_decision(
        project.id, title="Use SQLite", body="Store locally", rationale="Offline",
        author="user", source_ref="architecture.md", source_revision="abc",
    )
    knowledge.approve_decision(decision.id, actor="user")
    dialog = KnowledgeReviewDialog(store, project.id)
    qtbot.addWidget(dialog)

    assert "architecture.md" in dialog.details.toPlainText()
    assert "abc" in dialog.details.toPlainText()
    monkeypatch.setattr(
        "local_model_chat.ui.knowledge_dialog.QInputDialog.getText",
        lambda *_args, **_kwargs: ("Use SQLite WAL", True),
    )
    answers = iter((("Store locally with WAL", True), ("Concurrent readers", True)))
    monkeypatch.setattr(
        "local_model_chat.ui.knowledge_dialog.QInputDialog.getMultiLineText",
        lambda *_args, **_kwargs: next(answers),
    )

    dialog._correct()

    assert "Previously approved revision 1" in dialog.details.toPlainText()
    assert knowledge.approved_revision(decision.id).body == "Store locally"
    assert store.list_messages(conversation.id)[0].content == "Original conversation"
    dialog._approve()
    assert knowledge.approved_revision(decision.id).body == "Store locally with WAL"
