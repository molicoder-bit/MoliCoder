"""Conservative clarification checks for Build requests with no safe target."""

from __future__ import annotations

import re


def clarification_needed(request: str) -> str:
    normalized = re.sub(r"\s+", " ", request.strip().lower()).rstrip(".!? ")
    if re.match(r"^(?:please )?(?:deploy|publish)\b", normalized) and re.search(
        r"\b(?:to|into)\s+production\b", normalized
    ):
        return "Production deployment is not a supported Build action. Use an explicit deployment workflow after reviewing the local build."
    if re.fullmatch(
        r"(?:please )?(?:fix|change|update|improve|make better) (?:the |this )?(?:thing|stuff|it|something)",
        normalized,
    ):
        return "Clarify which file or behavior to change and what result you expect."

    delete = re.search(r"\b(?:delete|remove)\s+((?:this|the)\s+file|[\w./\\-]+)", normalized)
    if delete and re.search(r"\b(?:keep|leave)\s+it\s+unchanged\b", normalized):
        return "Clarify the conflict: the same file cannot be deleted and kept unchanged."
    if delete:
        target = delete.group(1)
        keep = re.search(r"\b(?:keep|leave)\s+([\w./\\-]+)\s+unchanged\b", normalized)
        if keep and keep.group(1) == target:
            return "Clarify the conflict: the same file cannot be deleted and kept unchanged."
    return ""
