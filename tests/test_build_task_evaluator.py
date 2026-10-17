"""Planner selection must not dictate implementation model or effort."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from local_model_chat.molicoder.planner.schema import parse_plan, PlanValidationError
from local_model_chat.molicoder.providers import ProviderResult, ProviderStatus, ZERO_COST_MODELS
from local_model_chat.molicoder.runtime import build_orchestrator
from local_model_chat.molicoder.task_routing import TaskRouter
from local_model_chat.orchestration.catalog import CapabilityCatalogService, Route
from test_build_plan_approval import approve, fixture, ScriptedExecutor


def plan(complexity='low'):
    return parse_plan({'objective': 'Implement addition', 'tasks': [{
        'key': 'addition', 'title': 'Implement and test addition', 'objective': 'Add a reusable addition function and tests',
        'risk': 'low', 'complexity': complexity, 'depends_on': [],
        'expected_paths': ['addition.py', 'test_addition.py'],
    }]})


@pytest.mark.parametrize('local,free,expected', [
    (True, True, ('ollama', 'molicoder:latest', None)),
    (False, True, ('kilo', ZERO_COST_MODELS['kilo'], None)),
    (False, False, ('codex', 'affordable-small', 'low')),
])
def test_runtime_independently_evaluates_each_task(tmp_path, monkeypatch, local, free, expected):
    root, store, project, _, _ = fixture(tmp_path)
    models = [dict(model='frontier-planner', description='Frontier workhorse for demanding tasks', effort_levels=['low','medium','high']),
              dict(model='affordable-small', description='Affordable for easier tasks', effort_levels=['low','medium'])]
    monkeypatch.setattr(CapabilityCatalogService, 'codex_models', staticmethod(lambda: models))
    store.set_project_preferences(project.id, provider='codex', strategy='HYBRID',
        model_overrides={'codex':'frontier-planner'}, trusted_providers=('codex','kilo') if free else ('codex',))
    # No startup catalog refresh: Build must still discover the installed local model.
    monkeypatch.setattr('local_model_chat.molicoder.runtime.create_provider', lambda name, **kw: SimpleNamespace(
        name=name, model=kw.get('model',''), probe=lambda: ProviderStatus(name, True, True, auth_verified=True)))
    scope = 'independent-task-routing'
    store.record_action_authorization(project_id=project.id, action='build_run',scope=scope, risk='external',decision='approved',
        actor='test', expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    runtime = build_orchestrator(store=store, project=project, provider_name='codex', model='frontier-planner',
        authorization_scope=scope, client=SimpleNamespace(tags=lambda: [{'name':'molicoder:latest'}] if local else []))
    assignment = runtime._task_assignments(plan(), 'HYBRID')['addition']
    assert (assignment['provider'], assignment['model'], assignment['effort']) == expected
    assert runtime.provider.model == 'frontier-planner'
    difficult = runtime._task_assignments(plan('high'), 'HYBRID')['addition']
    assert (difficult['model'], difficult['effort']) == ('frontier-planner','high')
    assert 'independently' in runtime._runner_policy('HYBRID')['selection_reason']


def test_execution_uses_reviewed_model_effort_and_fallback_not_planner(tmp_path):
    root, store, project, planner, runtime = fixture(tmp_path, 'hello.txt', 'hello\n')
    planner.effort = 'high'
    seen = []
    class Cheap(ScriptedExecutor):
        name = 'codex'
        model = 'small'
        effort = 'medium'
        def execute(self, *args, **kwargs):
            seen.append((self.name, self.model, self.effort))
            self.calls += 1
            return ProviderResult('failed', '', error='service unavailable')
    class Free(ScriptedExecutor):
        name = 'kilo'
        model = 'free'
        effort = None
        def execute(self, *args, **kwargs):
            seen.append((self.name, self.model, self.effort))
            return super().execute(*args, **kwargs)
    cheap, free = Cheap('hello.txt','hello\n'), Free('hello.txt','hello\n')
    runtime.task_providers = (cheap, free)
    routes = [Route('codex','small','codex', effort='low',cost_class='subscription'),
              Route('kilo','free','kilo',cost_class='free')]
    runtime.task_router = TaskRouter({'low':routes}, local_ready=False)
    prepared = runtime.run('Write hello.txt', mode='HYBRID')
    assert prepared.status == 'awaiting_approval', prepared.error
    saved = store.get_latest_plan_record(prepared.run_id)['payload']['task_assignments']['write']
    assert saved['model'] == 'small' and saved['fallback_model'] == 'free'
    policy = runtime._runner_policy('HYBRID')
    approve(store, prepared.run_id)
    result = runtime.execute_approved(prepared.run_id)
    assert result.status == 'ready_to_apply', result.error
    assert seen == [('codex','small','low'), ('kilo','free',None)]
    assert planner.calls == 0 and planner.effort == 'high' and cheap.effort == 'medium'
    assert runtime._runner_policy('HYBRID') == policy
    assert (result.integration_path/'hello.txt').read_text() == 'hello\n'
    assert not (root/'hello.txt').exists()


def test_failed_local_task_with_no_approved_fallback_never_calls_planner(tmp_path):
    from local_model_chat.molicoder.executor import ExecutionResult
    from local_model_chat.molicoder.repair import RepairOutcome
    _, store, _, planner, runtime = fixture(tmp_path)
    runtime.task_router = TaskRouter({'low':[Route('ollama','molicoder:latest','internal', cost_class='local')]}, local_ready=True)
    execution = ExecutionResult('failed', '', 1, 0, error='Cannot solve this task')
    runtime.local_executor = lambda *_: RepairOutcome('failed', 1, execution, (), 'Cannot solve this task', (execution,))
    prepared = runtime.run('Write hello.txt',mode='HYBRID')
    approve(store, prepared.run_id)
    result = runtime.execute_approved(prepared.run_id)
    assert result.status == 'needs_attention' and planner.calls == 0


def test_no_capable_executor_does_not_inherit_planner(tmp_path):
    _, _, _, _, runtime = fixture(tmp_path)
    runtime.task_router = TaskRouter({'high':[]},local_ready=False)
    with pytest.raises(ValueError, match='No allowed execution model'):
        runtime._task_assignments(plan('high'),'HYBRID')


def test_local_eligibility_uses_scope_and_complexity_not_incidental_words(tmp_path):
    from dataclasses import replace
    _, _, _, _, runtime = fixture(tmp_path)
    task = replace(plan().tasks[0], objective='Implement addition. Leave dependency manifests unchanged.')
    assert runtime._local_eligible(task, runtime.root)
    assert not runtime._local_eligible(replace(task, complexity='high'), runtime.root)
    assert not runtime._local_eligible(replace(task, expected_paths=('pyproject.toml',)), runtime.root)


@pytest.mark.parametrize('complexity', ['impossible', [], None])
def test_invalid_complexity_rejected(complexity):
    payload = plan().to_dict()
    payload['tasks'] = [dict(payload['tasks'][0], complexity=complexity)]
    with pytest.raises(PlanValidationError, match='complexity'):
        parse_plan(payload)
