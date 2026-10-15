from __future__ import annotations

import json

from ..storage import ChatStore


def render_task_evidence(store: ChatStore, run_id: int) -> str:
    """Summarize persisted Build evidence without requiring raw JSONL inspection."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError("run does not exist")
    events = store.list_events(run_id)
    lines = [f"Run {run_id} · {run['status']}", f"Provider: {run['provider'] or 'unknown'}; model: {run['model'] or 'unknown'}"]
    if run.get("recovery_reason"):
        lines.append(f"Recovery reason: {run['recovery_reason']}")
    for task in store.list_run_tasks(run_id):
        lines.extend(("", f"{task.key} — {task.title}: {task.status}; attempts {task.attempts}"))
        lines.append(f"Worktree: {task.worktree or 'unavailable'}")
        completion = store.get_task_completion(task.id)
        if completion:
            lines.append(f"Checkpoint: {completion.revision}")
            lines.append(f"Verification: {completion.validation_summary}")
            lines.append("Checks: " + ", ".join(completion.checks))
            lines.append("Output: " + (", ".join(completion.output_artifacts) or "none"))
            if completion.unmet_criteria:
                lines.append("Needs review: " + "; ".join(completion.unmet_criteria))
        for event in events:
            if event.task_id != task.id:
                continue
            payload = json.loads(event.payload_json)
            if event.kind == "task_recovery_decision":
                lines.append(
                    f"Recovery: {payload.get('category', 'unknown')} → {payload.get('action', 'unknown')}"
                )
            elif event.kind == "failed_attempt_preserved":
                lines.append(f"Failed attempt saved: {payload.get('stash_commit', 'unknown')}")
            elif event.kind in {"provider_takeover", "model_handoff"}:
                lines.append(
                    f"Model handoff: {payload.get('provider', 'unknown')} — {payload.get('reason', 'unknown')}"
                )
    return "\n".join(lines)
