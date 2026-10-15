import pytest

from local_model_chat.inference.fallback import RouteCooldowns, classify_provider_failure


@pytest.mark.parametrize("error,category,action", [
    ("authentication required", "auth", "wait_for_login"),
    ("quota exceeded", "quota", "wait_for_reset"),
    ("model not found", "model", "review_model"),
    ("connection refused", "outage", "cooldown"),
    ("patch failed validation", "execution", "retry_if_budget_allows"),
])
def test_failure_classification(error, category, action) -> None:
    decision = classify_provider_failure("needs_attention", error)
    assert decision.category == category
    assert decision.action == action


def test_cooldown_blocks_immediate_repeat_for_outage() -> None:
    cooldowns = RouteCooldowns()
    cooldowns.apply("provider", classify_provider_failure("needs_attention", "connection refused"))
    assert not cooldowns.available("provider")
