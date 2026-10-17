import json
from types import SimpleNamespace
import pytest
from local_model_chat.kandev.mirror import KandevPlanMirror
from test_build_plan_approval import fixture, approve
from test_build_level7_kandev_board import task


class Api:
    def __init__(self):
        self.config = SimpleNamespace(workspace_id='workspace')
        self.tasks = {}
    def probe_health(self): pass
    def find_by_external_id(self, key): return self.tasks.get(key)
    def create_mirror(self, key, description, *, blocked_by):
        value = dict(id='remote-' + str(len(self.tasks)), external_id=key, description=description,
                     blocked_by=blocked_by, start_agent=False, start_when_unblocked=False, primary_session_id=None)
        self.tasks[key] = value
        return value
    def update_mirror(self, remote, key, **fields):
        assert self.tasks[key]['id'] == remote
        self.tasks[key].update(fields)


def test_internal_plan_is_mirrored_with_dependencies_assignments_and_restart_identity(tmp_path):
    root, store, project, _, _ = fixture(tmp_path)
    run = store.create_run(project.id, 'Two meaningful tasks', 'HYBRID', provider='codex')
    plan = {'objective': 'Feature and user documentation', 'tasks': [task('feature'), task('docs', 'feature')],
        'task_assignments': {'feature': {'provider': 'opencode', 'model': 'opencode/big-pickle'}}}
    store.save_plan(run, 'generated', 'codex', plan)
    ids = {t['key']: store.create_run_task(run, t['key'], t['title'], t['objective'], t['risk']) for t in plan['tasks']}
    api = Api(); mirror = KandevPlanMirror(store, project, api=api)
    with pytest.raises(PermissionError): mirror.sync(run)
    approve(store, run); mirror.sync(run)
    remote = list(api.tasks.values())
    assert len(remote) == 2 and remote[1]['blocked_by'] == (remote[0]['id'],)
    assert all(not t['start_agent'] and not t['primary_session_id'] and not t['start_when_unblocked'] for t in remote)
    assert 'opencode/big-pickle' in remote[0]['description']
    store.update_run_task(ids['feature'], 'ready', 0)
    store.update_run_task(ids['feature'], 'running', 1)
    KandevPlanMirror(store, project, api=api).sync(run)
    assert len(api.tasks) == 2 and remote[0]['state'] == 'IN_PROGRESS'
