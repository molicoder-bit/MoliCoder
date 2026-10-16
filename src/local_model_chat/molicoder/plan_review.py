from __future__ import annotations

from collections import defaultdict

from .planner.schema import Plan


def plan_review_warnings(plan: Plan) -> tuple[str, ...]:
    """Flag repeated task scopes for human review without changing the approved graph."""
    groups: dict[tuple, list[str]] = defaultdict(list)
    for task in plan.tasks:
        if not task.expected_paths:
            continue
        signature = (
            tuple(sorted(task.expected_paths)),
            task.verification_checks,
            task.acceptance_criteria,
            tuple(tuple(sorted(check.items())) for check in task.artifact_checks),
        )
        groups[signature].append(task.key)
    return tuple(
        "Possible excessive fragmentation: tasks " + ", ".join(keys)
        + " share the same file scope and checks. Review whether they form one atomic change; keep required verification."
        for keys in groups.values() if len(keys) >= 3
    )
