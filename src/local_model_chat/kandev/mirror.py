"""Durable projection of internal tasks. This adapter never launches an agent."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from uuid import uuid4

from ..molicoder.plan_approval import require_approved_plan
from ..molicoder.planner.schema import parse_plan
from .ui_config import configured_kandev_adapter


class KandevPlanMirror:
    EVENTS = {'task_started', 'task_completed', 'task_failed', 'validation_started', 'validation_finished',
              'repair_started', 'model_handoff', 'run_completed', 'run_needs_attention', 'run_failed', 'run_cancelled'}

    def __init__(self, store, project, *, api=None):
        self.store, self.project = store, project
        self.api = api
        self._lock = threading.Lock()
        self._pending = set()
        self._thread = None
        profile = store.get_setting('orchestration.profile_id')
        if not profile:
            profile = uuid4().hex
            store.set_setting('orchestration.profile_id', profile)
        self.namespace = profile + ':' + hashlib.sha256(project.path.encode()).hexdigest()[:12]

    def observe(self, run_id, kind, payload, task_id=None):
        if kind not in self.EVENTS:
            return
        with self._lock:
            self.store.set_setting(f'kandev.mirror.pending.{run_id}', str(self.project.id))
            self._pending.add(run_id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._drain, daemon=True)
                self._thread.start()

    def _drain(self):
        while True:
            with self._lock:
                if not self._pending:
                    self._thread = None
                    return
                run_id = self._pending.pop()
            try:
                self.sync(run_id)
            except Exception as exc:
                # State and mappings remain durable; a later event/reconnect retries by external ID.
                self.store.set_setting(f'kandev.mirror.status.{run_id}', json.dumps({'state': 'stale', 'reason': str(exc)[:500]}))

    def sync(self, run_id):
        run = self.store.get_run(run_id)
        if not run or int(run['project_id']) != self.project.id or run['provider'] == 'kandev':
            raise ValueError('Only internal runs for this project can be mirrored')
        mapping_key = f'kandev.mirror.mapping.{run_id}'
        mapping = json.loads(self.store.get_setting(mapping_key, '{}'))
        if not mapping:
            require_approved_plan(self.store, run_id)
        saved = self.store.get_latest_plan_record(run_id)
        if saved is None:
            raise ValueError('The run has no saved plan')
        plan = parse_plan(saved['payload'])
        rows = {task.key: task for task in self.store.list_run_tasks(run_id)}
        assignments = saved['payload'].get('task_assignments', {})
        api = self.api or configured_kandev_adapter(self.store, project_id=self.project.id).api
        api.probe_health()
        remote_ids = {}
        for task in plan.ordered_tasks():
            row = rows[task.key]
            external_id = f'molicoder:mirror:{self.namespace}:{run_id}:{task.key}'
            snapshot = {'execution_owner': 'MoliCoder internal executor', 'project': self.project.path,
                'run_id': run_id, 'run_status': run['status'], 'task_id': row.id, 'task_key': task.key,
                'task_state': row.status, 'assignment': assignments.get(task.key, {}),
                'depends_on': list(task.depends_on), 'acceptance_criteria': list(task.acceptance_criteria),
                'verification_checks': list(task.verification_checks), 'worktree': run.get('worktree'),
                'note': 'Status mirror only. Agent launch and start-when-unblocked are disabled. Apply is a separate operation.'}
            description = task.objective + '\n\nMoliCoder execution record:\n' + json.dumps(snapshot, indent=2)
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            for action in ('kandev_task_create', 'kandev_task_update'):
                self.store.record_action_authorization(project_id=None, action=action,
                    scope=f'{api.config.workspace_id}:{external_id}', risk='external', decision='approved',
                    actor='project_configured_kandev_mirroring', reason='Project board connection and approved internal Build plan', expires_at=expiry)
            remote = api.find_by_external_id(external_id)
            if remote is None:
                remote = api.create_mirror(external_id, description, blocked_by=tuple(remote_ids[key] for key in task.depends_on))
            if remote.get('external_id') != external_id or not remote.get('id'):
                raise ValueError('Kandev mirror identity needs reconciliation')
            remote_ids[task.key] = remote['id']
            mapping[task.key] = remote['id']
            self.store.set_setting(mapping_key, json.dumps(mapping))
            state = ('COMPLETED' if row.status == 'completed' else 'FAILED' if row.status in {'failed', 'needs_attention', 'review_required'} else
                     'CANCELLED' if row.status == 'cancelled' else 'IN_PROGRESS' if row.status in {'running', 'retrying', 'verifying'} else 'CREATED')
            api.update_mirror(remote['id'], external_id, state=state,
                title=f'{row.status} · {task.title}'[:180], description=description)
        self.store.set_setting(f'kandev.mirror.status.{run_id}', json.dumps({'state': 'synced', 'tasks': len(remote_ids)}))
        self.store.set_setting(f'kandev.mirror.pending.{run_id}', '')

    def replay_pending(self):
        """Reconnect retries durable projections; find-by-external-ID prevents duplicates."""
        from contextlib import closing
        with closing(self.store._connect()) as db:
            keys = db.execute("SELECT key,value FROM settings WHERE key LIKE 'kandev.mirror.pending.%' AND value=?", (str(self.project.id),)).fetchall()
        for row in keys:
            self.observe(int(row['key'].rsplit('.', 1)[1]), 'run_completed', {})
