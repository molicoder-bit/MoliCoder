from __future__ import annotations

import re
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FailureAction:
    category: str
    action: str
    retry_same_route: bool
    cooldown_seconds: int = 0


def classify_provider_failure(status: str, error: str) -> FailureAction:
    message = error.lower()[:4000]
    if status == "cancelled":
        return FailureAction("cancelled", "stop", False)
    if re.search(r"\b(unauthorized|authentication|login required|invalid api key|401|403)\b", message):
        return FailureAction("auth", "wait_for_login", False)
    if re.search(r"\b(quota|rate.limit|too many requests|429|allowance exhausted)\b", message):
        return FailureAction("quota", "wait_for_reset", False, 900)
    if re.search(r"\b(model not found|unknown model|unsupported model)\b", message):
        return FailureAction("model", "review_model", False)
    if re.search(r"\b(timed out|timeout|connection refused|service unavailable|503|502)\b", message):
        return FailureAction("outage", "cooldown", False, 120)
    return FailureAction("execution", "retry_if_budget_allows", True)


class RouteCooldowns:
    def __init__(self) -> None:
        self._until: dict[str, float] = {}

    def apply(self, route_id: str, action: FailureAction) -> None:
        if action.cooldown_seconds:
            self._until[route_id] = max(
                self._until.get(route_id, 0), time.monotonic() + action.cooldown_seconds,
            )

    def available(self, route_id: str) -> bool:
        return time.monotonic() >= self._until.get(route_id, 0)
