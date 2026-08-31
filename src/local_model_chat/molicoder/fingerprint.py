from __future__ import annotations

import hashlib
import re


def failure_fingerprint(value: str) -> str:
    normalized = value.lower().replace("\\", "/")
    normalized = re.sub(r"[a-z]:/(?:[^\s:]+/)*([^/\s:]+)", r"\1", normalized)
    normalized = re.sub(r":\d+", ":<line>", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?s\b", "<time>", normalized)
    normalized = re.sub(r"\b0x[0-9a-f]+\b", "<address>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def classify_progress(history: list[str] | tuple[str, ...]) -> str:
    if len(history) >= 3 and len(set(history[-3:])) == 1:
        return "stuck"
    if len(history) >= 4 and history[-4] == history[-2] and history[-3] == history[-1] and history[-1] != history[-2]:
        return "oscillating"
    if len(history) < 2:
        return "insufficient"
    return "progress" if history[-1] != history[-2] else "unchanged"
