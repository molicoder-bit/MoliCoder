import pytest

from local_model_chat.task_packet import TaskHandoff


def test_retry_handoff_keeps_verified_state_and_bounded_failure() -> None:
    packet = TaskHandoff(
        packet_version=1, project_id=7, run_id=9, task_id=12, base_commit="abc123",
        goal="Implement search", plan_objective="Add search", task_key="T1",
        task_title="Index files", task_objective="Build index", expected_paths=("src/index.py",),
        verified_paths=("src/search.py", "tests/test_search.py"),
        approved_constraints=("Decision #4 r2: keep local state",),
        failure_evidence="test_search failed", remaining_attempts=1,
    )

    prompt = packet.render(project_kind="python")

    assert "base abc123" in prompt
    assert "Decision #4 r2" in prompt
    assert "src/search.py" in prompt
    assert "Previous validation failed:\ntest_search failed" in prompt
    assert "remaining attempts 1" in prompt
    with pytest.raises(ValueError, match="escapes"):
        TaskHandoff(
            1, 7, 9, 12, "abc123", "goal", "plan", "T1", "title", "objective",
            ("../secret",), (), (), "", 1,
        )
