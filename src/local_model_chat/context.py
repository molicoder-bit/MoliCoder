from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .config import SAFE_CONTEXT_FRACTION, WARNING_CONTEXT_FRACTION
from .domain import Message


MESSAGE_OVERHEAD_TOKENS = 8


def estimate_tokens(text: str) -> int:
    """Return a conservative estimate for Qwen-family tokenization.

    Ollama reports exact counts only after evaluation, so the UI labels this as
    an estimate and uses UTF-8 bytes / 3 plus per-message overhead.
    """
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def estimate_messages(messages: Iterable[Message | dict[str, str]]) -> int:
    total = 0
    for message in messages:
        content = message.content if isinstance(message, Message) else message.get("content", "")
        total += MESSAGE_OVERHEAD_TOKENS + estimate_tokens(content)
    return total


@dataclass(frozen=True, slots=True)
class ContextBudget:
    estimated_prompt_tokens: int
    reserved_output_tokens: int
    context_window: int

    @property
    def total_reserved(self) -> int:
        return self.estimated_prompt_tokens + self.reserved_output_tokens

    @property
    def fraction(self) -> float:
        return min(1.0, self.total_reserved / max(1, self.context_window))

    @property
    def warning(self) -> bool:
        return self.fraction >= WARNING_CONTEXT_FRACTION

    @property
    def blocked(self) -> bool:
        return self.total_reserved > int(self.context_window * SAFE_CONTEXT_FRACTION)

    @property
    def remaining(self) -> int:
        return max(0, int(self.context_window * SAFE_CONTEXT_FRACTION) - self.total_reserved)


def calculate_budget(
    messages: Iterable[Message | dict[str, str]],
    pending_text: str,
    context_window: int,
    reserved_output_tokens: int,
) -> ContextBudget:
    prompt = estimate_messages(messages) + MESSAGE_OVERHEAD_TOKENS + estimate_tokens(pending_text)
    return ContextBudget(prompt, reserved_output_tokens, context_window)

