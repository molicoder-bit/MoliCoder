from local_model_chat.context import ContextBudget, estimate_tokens


def test_token_estimate_handles_empty_and_unicode() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello") == 2
    assert estimate_tokens("á") == 1


def test_budget_warns_at_80_percent_and_blocks_above_95_percent() -> None:
    warning = ContextBudget(7000, 1000, 10_000)
    blocked = ContextBudget(8501, 1000, 10_000)
    assert warning.warning
    assert not warning.blocked
    assert blocked.blocked
    assert blocked.remaining == 0
