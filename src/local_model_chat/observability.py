from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .molicoder.providers import redact_text
from .storage import ChatStore


TRACE_RETENTION_DAYS = 30
_EVENT_FIELDS = {
    "run_stage": ("stage",),
    "task_state_changed": ("from", "to", "attempts", "revision"),
    "provider_attempt_started": ("key", "provider", "attempt", "maximum_attempts"),
    "validation_finished": ("key", "attempt", "passed"),
    "task_completed": ("key", "attempts", "checkpoint"),
    "task_recovery_decision": ("key", "category", "action", "retry_allowed", "fingerprint"),
    "model_handoff": ("key", "from_provider", "provider", "checkpoint", "remaining_attempts"),
    "model_review_finished": ("key", "revision"),
    "model_review_skipped": ("key", "revision"),
    "run_completed": ("status", "completed_tasks", "total_tasks"),
    "run_needs_attention": ("status", "completed_tasks", "total_tasks"),
}


def _timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.astimezone().astimezone(timezone.utc)


def _safe_value(value: object) -> str | int | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/#-]{1,100}", value):
        return redact_text(value)
    return None


class TraceExporter:
    """Export bounded, allowlisted correlation data without prompts or provider output."""

    def __init__(self, store: ChatStore, *, retention_days: int = TRACE_RETENTION_DAYS) -> None:
        if not 1 <= retention_days <= 365:
            raise ValueError("trace retention must be between one and 365 days")
        self.store = store
        self.retention_days = retention_days

    def export_run(self, run_id: int, *, now: datetime | None = None) -> Path:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError("run does not exist")
        project = self.store.get_project(int(run["project_id"]))
        if project is None:
            raise ValueError("run project is unavailable")
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = now - timedelta(days=self.retention_days)
        root = Path(project.path).resolve()
        output_dir = root / ".molicoder" / "trace-exports"
        if not output_dir.resolve().is_relative_to(root):
            raise PermissionError("trace export location escapes the project")
        output_dir.mkdir(parents=True, exist_ok=True)
        self._prune(output_dir, cutoff)
        records: list[dict] = []
        for event in self.store.list_events(run_id):
            fields = _EVENT_FIELDS.get(event.kind)
            if fields is None or _timestamp(event.created_at) < cutoff:
                continue
            payload = json.loads(event.payload_json)
            safe_payload = {
                key: value for key in fields
                if (value := _safe_value(payload.get(key))) is not None
            }
            records.append({
                "type": "run_event", "run_id": run_id, "task_id": event.task_id,
                "kind": event.kind, "timestamp": event.created_at, "fields": safe_payload,
            })
        with closing(self.store._connect()) as connection:
            usage = connection.execute(
                """SELECT r.request_id, r.run_id, r.offering_id, r.purpose, r.created_at,
                          e.status, e.input_tokens, e.output_tokens, e.cost_accuracy
                   FROM inference_requests r LEFT JOIN inference_usage_events e ON e.id = (
                       SELECT MAX(id) FROM inference_usage_events WHERE request_id = r.request_id
                   ) WHERE r.run_id = ? ORDER BY r.created_at LIMIT 50000""",
                (run_id,),
            ).fetchall()
        for row in usage:
            if _timestamp(row["created_at"]) < cutoff:
                continue
            records.append({
                "type": "inference_usage", "run_id": run_id,
                "request_id": _safe_value(row["request_id"]),
                "offering_id": _safe_value(row["offering_id"]),
                "purpose": _safe_value(row["purpose"]),
                "timestamp": row["created_at"],
                "status": _safe_value(row["status"]),
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cost_accuracy": _safe_value(row["cost_accuracy"]),
            })
        target = output_dir / f"trace-{run_id}-{now.strftime('%Y%m%dT%H%M%S%fZ')}.jsonl"
        with target.open("x", encoding="utf-8", newline="") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return target

    @staticmethod
    def _prune(output_dir: Path, cutoff: datetime) -> None:
        for path in output_dir.glob("trace-*.jsonl"):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(output_dir.resolve()):
                continue
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                path.unlink()
