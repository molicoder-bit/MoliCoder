import pytest
from pathlib import Path
from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderStatus, ProviderUnavailable
from test_build_plan_approval import fixture, approve


def test_active_desktop_runner_accepts_six_meaningful_tasks(tmp_path):
    root, store, project, provider, runner = fixture(tmp_path)
    class Planner:
        provider = 'scripted'
        def plan(self, packet):
            return parse_plan({'objective': packet.objective, 'tasks': [
                {'key': 'task' + str(n), 'title': 'Feature slice ' + str(n), 'objective': 'Implement and test feature slice ' + str(n),
                 'risk': 'low', 'depends_on': [], 'expected_paths': [f'feature{n}.txt'],
                 'acceptance_criteria': ['Configured validation must pass'], 'verification_checks': ['project-default']}
                for n in range(6)]})
    runner.planner = Planner()
    result = runner.run('Implement six independent feature slices', mode='HYBRID')
    assert result.status == 'awaiting_approval' and result.total_tasks == 6
    assert len(store.get_latest_plan_record(result.run_id)['payload']['task_assignments']) == 6
    assert provider.calls == 0


def test_planning_failure_uses_trusted_reasoning_route_and_binds_actual_model(tmp_path):
    root, store, project, original, runner = fixture(tmp_path, 'hello.txt', 'hello\n')
    good_plan = runner.planner.plan
    class Unavailable:
        name = 'codex'; model = 'unavailable-model'
        def plan(self, packet): raise ProviderUnavailable('quota exhausted')
        def cancel(self): pass
    class Alternative:
        name = 'claude'; model = 'working-model'; effort = None
        def probe(self): return ProviderStatus('claude', True, True, auth_verified=True)
        def plan(self, packet): return good_plan(packet)
        def execute(self, path, prompt, **kwargs): return original.execute(path, prompt, **kwargs)
        def cancel(self): pass
    runner.provider = runner.planner = Unavailable()
    runner.planning_providers = (Alternative(),)
    result = runner.run('Write hello.txt', mode='FRONTIER_ONLY')
    assert result.status == 'awaiting_approval'
    assert store.get_run(result.run_id)['provider'] == 'claude'
    assert store.get_run(result.run_id)['model'] == 'working-model'
    payload = store.get_latest_plan_record(result.run_id)['payload']
    assert payload['task_assignments']['write']['model'] == 'working-model'
    approve(store, result.run_id)
    done = runner.execute_approved(result.run_id)
    assert done.status == 'ready_to_apply', done.error
    assert (done.integration_path / 'hello.txt').read_text() == 'hello\n'
    assert not (root / 'hello.txt').exists()


def test_planner_authorization_failure_never_uses_another_route(tmp_path):
    _, _, _, _, runner = fixture(tmp_path)
    class Denied:
        def plan(self, packet): raise PermissionError('Approval expired')
        def cancel(self): pass
    class Alternative:
        name = 'alternative'; model = 'unused'
        def probe(self): pytest.fail('Must not evade a permission boundary')
    runner.planner = Denied(); runner.planning_providers = (Alternative(),)
    result = runner.run('Write hello.txt')
    assert result.status == 'needs_attention' and 'Approval expired' in result.error
