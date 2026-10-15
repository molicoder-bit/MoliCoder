from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    id: str
    category: str
    privacy: str
    prompt: str
    acceptance_kind: str
    expected: tuple[str, ...]

    def accepts(self, response: str) -> bool:
        lower = response.lower()
        if self.acceptance_kind == "contains":
            return self.expected[0].lower() in lower
        return all(value.lower() in lower for value in self.expected)


def load_benchmark_cases(path: Path | None = None) -> tuple[BenchmarkCase, ...]:
    source = path or Path(__file__).with_name("fixtures.json")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) != 6:
        raise ValueError("benchmark set must contain six fixed cases")
    cases: list[BenchmarkCase] = []
    for item in raw:
        acceptance = item.get("acceptance", {})
        kind = acceptance.get("kind")
        expected = (acceptance.get("value"),) if kind == "contains" else acceptance.get("values")
        if (item.get("category") not in {"chat", "retrieval", "code_review", "patch", "recovery", "architecture"}
                or item.get("privacy") not in {"public", "private", "restricted"}
                or kind not in {"contains", "contains_all"}
                or not isinstance(expected, (tuple, list)) or not expected
                or not all(isinstance(value, str) and value.strip() for value in expected)
                or not isinstance(item.get("prompt"), str) or not item["prompt"].strip()):
            raise ValueError("invalid benchmark fixture")
        cases.append(BenchmarkCase(item["id"], item["category"], item["privacy"],
                                   item["prompt"], kind, tuple(expected)))
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate benchmark case ID")
    return tuple(cases)
