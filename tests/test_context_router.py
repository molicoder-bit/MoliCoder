import pytest

from local_model_chat.context_router import build_context_packet, select_context, verify_context_freshness
from local_model_chat.knowledge import ProjectKnowledge
from local_model_chat.molicoder.repo_index import build_repository_index
from local_model_chat.storage import ChatStore


def test_context_selection_finds_related_code_test_and_decision_without_whole_repo(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("def authenticate(user):\n    return bool(user)\n", encoding="utf-8")
    (root / "billing.py").write_text("def charge():\n    pass\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_auth.py").write_text("def test_authenticate():\n    pass\n", encoding="utf-8")
    (tests / "test_billing.py").write_text("def test_charge():\n    pass\n", encoding="utf-8")
    store = ChatStore(tmp_path / "context.sqlite3")
    project = store.upsert_project("Repo", str(root), "python")
    knowledge = ProjectKnowledge(store)
    decision = knowledge.propose_decision(
        project.id, title="Authentication policy", body="Keep authentication local",
        rationale="Protect credentials", author="user",
    )
    knowledge.approve_decision(decision.id, actor="user")

    selection = select_context(
        build_repository_index(root), knowledge.approved_decisions(project.id),
        "Evaluate authentication implementation",
    )

    assert selection.files == ("auth.py",)
    assert selection.tests == ("tests/test_auth.py",)
    assert selection.decision_ids == (decision.id,)
    assert "billing.py" not in selection.files


def test_context_packet_preserves_decision_and_bounds_file_excerpts(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("def authenticate():\n" + "    return True\n" * 1000, encoding="utf-8")
    store = ChatStore(tmp_path / "packet.sqlite3")
    project = store.upsert_project("Repo", str(root), "python")
    knowledge = ProjectKnowledge(store)
    decision = knowledge.propose_decision(
        project.id, title="Authentication rule", body="Authentication stays local",
        rationale="Protect credentials", author="user",
    )
    knowledge.approve_decision(decision.id, actor="user")
    snapshot = build_repository_index(root)
    decisions = knowledge.approved_decisions(project.id)
    selection = select_context(snapshot, decisions, "Evaluate authentication")

    packet = build_context_packet(
        snapshot, selection, decisions, "Evaluate authentication",
        context_window=350, reserved_output_tokens=80, max_file_tokens=100,
    )

    assert packet.estimated_input_tokens + packet.reserved_output_tokens <= int(350 * 0.95)
    assert any(item.kind == "decision" and "Authentication stays local" in item.content for item in packet.items)
    assert any(item.kind == "code" and item.truncated for item in packet.items)
    assert verify_context_freshness(store, project.id, packet) == ()
    with pytest.raises(ValueError, match="approved decisions exceed"):
        build_context_packet(
            snapshot, selection, decisions, "Evaluate authentication",
            context_window=50, reserved_output_tokens=35,
        )
    (root / "auth.py").write_text("def authenticate(): return False", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since indexing"):
        build_context_packet(
            snapshot, selection, decisions, "Evaluate authentication",
            context_window=350, reserved_output_tokens=80,
        )
    assert "repository source revision changed" in verify_context_freshness(store, project.id, packet)[0]


def test_context_packet_detects_approved_decision_revision_change(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("def authenticate(): pass", encoding="utf-8")
    store = ChatStore(tmp_path / "decision-stale.sqlite3")
    project = store.upsert_project("Repo", str(root), "python")
    knowledge = ProjectKnowledge(store)
    decision = knowledge.propose_decision(
        project.id, title="Authentication", body="Keep auth local", rationale="Privacy", author="user",
    )
    knowledge.approve_decision(decision.id, actor="user")
    snapshot = build_repository_index(root)
    decisions = knowledge.approved_decisions(project.id)
    selection = select_context(snapshot, decisions, "Evaluate authentication")
    packet = build_context_packet(
        snapshot, selection, decisions, "Evaluate authentication",
        context_window=500, reserved_output_tokens=80,
    )
    knowledge.correct_decision(
        decision.id, title="Authentication", body="Use auth service", rationale="New plan", author="user",
    )
    knowledge.approve_decision(decision.id, actor="user")

    assert any("approved decision" in issue for issue in verify_context_freshness(store, project.id, packet))
