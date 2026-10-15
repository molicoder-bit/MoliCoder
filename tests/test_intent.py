import pytest

from local_model_chat.intent import IntentKind, classify_intent


@pytest.mark.parametrize(
    ("message", "kind", "operation"),
    [
        ("Hi", IntentKind.CONVERSATION, None),
        ("What task are we on?", IntentKind.RETRIEVAL, "task_status"),
        ("What commit are we on?", IntentKind.RETRIEVAL, "git_version"),
        ("Show changed files", IntentKind.RETRIEVAL, "changed_files"),
        ("Evaluate authentication", IntentKind.ANALYSIS, None),
        ("Run tests", IntentKind.TOOL, None),
        ("Change the color", IntentKind.MUTATION, None),
        ("Implement search", IntentKind.EXECUTION, None),
        ("Install Python", IntentKind.ADMINISTRATION, None),
    ],
)
def test_intent_taxonomy(message, kind, operation) -> None:
    decision = classify_intent(message)
    assert decision.kind == kind
    assert decision.operation == operation


def test_followup_and_ambiguous_text_do_not_trigger_deterministic_action() -> None:
    for text in ("And now?", "What version are we on?", "Can you implement the current task?"):
        decision = classify_intent(text)
        assert decision.operation is None
        assert decision.confidence < 0.95
