"""Prepare a capped custom chat request before centralized policy dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .budget import BudgetManager
from .contracts import InferencePurpose, InferenceRequest
from .registry import ProviderRegistry


@dataclass(frozen=True, slots=True)
class CustomChatPreview:
    offering_id: str
    model_id: str
    max_input_tokens: int
    max_output_tokens: int
    worst_case_usd: float | None


def preview_custom_chat(
    registry: ProviderRegistry, offering_id: str, prompt: str,
) -> CustomChatPreview:
    offering = registry.get_offering(offering_id)
    if offering is None or not offering.id.startswith("custom:"):
        raise ValueError("registered custom route is required")
    if not prompt.strip() or len(prompt.encode("utf-8")) > 4096:
        raise ValueError("test prompt must be nonempty and at most 4096 UTF-8 bytes")
    context = offering.capabilities.get("context_window")
    output = offering.capabilities.get("max_output_tokens")
    if not isinstance(context, int) or not isinstance(output, int):
        raise ValueError("verified model token limits are required")
    input_cap = len(prompt.encode("utf-8")) + 256
    output_cap = min(output, 256)
    if input_cap + output_cap > context:
        raise ValueError("test prompt exceeds the configured context window")
    worst_cost = None
    if offering.offering_class in {"metered", "trial"}:
        if offering.input_usd_per_million is None or offering.output_usd_per_million is None:
            raise ValueError("paid route price is unknown")
        worst_cost = (
            input_cap * offering.input_usd_per_million
            + output_cap * offering.output_usd_per_million
        ) / 1_000_000
        if worst_cost <= 0:
            raise ValueError("paid route requires a positive worst-case price")
    return CustomChatPreview(
        offering_id, offering.model_id, input_cap, output_cap, worst_cost,
    )


def prepare_custom_chat(
    registry: ProviderRegistry, project_id: int, offering_id: str, prompt: str,
    *, sensitivity: str = "private",
) -> InferenceRequest:
    if sensitivity not in {"public", "private", "restricted"}:
        raise ValueError("invalid project data sensitivity")
    preview = preview_custom_chat(registry, offering_id, prompt)
    request_id = str(uuid4())
    reservation = None
    if preview.worst_case_usd is not None:
        reservation = BudgetManager(registry.store).reserve(
            request_id, project_id, preview.worst_case_usd,
        )
    return InferenceRequest(
        request_id, project_id, offering_id, preview.model_id, InferencePurpose.CHAT,
        ({"role": "user", "content": prompt},), sensitivity, (),
        preview.max_input_tokens, preview.max_output_tokens, 60,
        reservation_id=reservation,
    )
