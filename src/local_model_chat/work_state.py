"""Authoritative operation snapshots shared by workspace status consumers."""
from dataclasses import dataclass


TERMINAL = frozenset({'completed', 'failed', 'needs_attention', 'cancelled', 'ready_to_apply', 'interrupted'})
WAITING = frozenset({'paused', 'waiting_auth', 'waiting_quota', 'awaiting_external'})


@dataclass(frozen=True)
class WorkSnapshot:
    operation_id: str
    mode: str
    project_id: int | None = None
    conversation_id: int | None = None
    run_id: int | None = None
    task_id: int | None = None
    phase: str = 'idle'
    outcome: str = ''
    reason: str = ''
    provider: str = ''
    requested_model: str = ''
    last_event_at: str = ''
    active_call: dict | None = None
    last_completed_call: dict | None = None

    @property
    def running(self):
        return self.phase != 'idle' and self.phase not in TERMINAL | WAITING

    @classmethod
    def from_run(cls, store, run_id):
        run = store.get_run(run_id)
        if run is None:
            return None
        tasks = store.list_run_tasks(run_id)
        events = store.list_events(run_id)
        import json
        last_call = next((json.loads(e.payload_json) for e in reversed(events) if e.kind == 'provider_finished'), None)
        active_call = None
        for event in reversed(events):
            if event.kind == 'provider_finished':
                break
            if event.kind == 'provider_started':
                active_call = json.loads(event.payload_json)
                break
        active = next((task for task in tasks if task.status in {'running', 'verifying', 'retrying'}), None)
        return cls(f'run:{run_id}', 'build', run['project_id'], run_id=run_id,
                   task_id=active.id if active else None, phase=run['status'],
                   outcome=run['status'] if run['status'] in TERMINAL else '',
                   reason=run.get('recovery_reason') or '', provider=run.get('provider') or '',
                   requested_model=run.get('model') or '',
                   last_event_at=events[-1].created_at if events else run.get('ended_at') or run['started_at'],
                   active_call=active_call if run['status'] not in TERMINAL else None,
                   last_completed_call=last_call)

    def accepts(self, operation_id):
        return self.operation_id == operation_id and not self.outcome
