import json
from dataclasses import replace

import pytest

from local_model_chat.orchestration.catalog import CapabilityCatalogService
from local_model_chat.orchestration.routing import ModelEvidence, WorkRequirements, assess
from local_model_chat.storage import ChatStore
from test_request_orchestration import Client, evaluation, service, turn


def catalog_fixture(tmp_path, trusted=('codex', 'opencode', 'kilo')):
    store = ChatStore(tmp_path / 'profile.db')
    project = store.upsert_project('Sample', str(tmp_path), 'generic')
    store.set_project_preferences(project.id, provider='codex', strategy='HYBRID', trusted_providers=trusted)
    catalog = CapabilityCatalogService(store, Client([])); catalog.refresh(discover_runtimes=False)
    facts = catalog.cached()
    facts.update(runtimes=[{'tool_id': p, 'installed': True} for p in ('codex', 'opencode', 'kilo')],
        cli_models={'codex': [
            {'model': 'expensive-first', 'description': 'Latest workhorse for coding', 'effort_levels': ['low','medium','high']},
            {'model': 'small-capable', 'description': 'Fast and affordable model for easier work', 'effort_levels': ['low','medium','high']}]})
    store.set_setting(catalog.KEY, json.dumps(facts))
    return catalog, project


@pytest.mark.parametrize('capabilities', [['text_generation'], ['read_file'], ['write_file'], ['list_directory'], ['run_command']])
def test_different_simple_work_uses_the_same_cost_and_capability_evaluator(tmp_path, capabilities):
    catalog, project = catalog_fixture(tmp_path)
    spec = evaluation(); spec['required_capabilities'] = capabilities
    routes, _ = catalog.routes(project, 'molicoder:latest', spec=spec)
    assert routes[0].provider == 'ollama'
    assert {r.provider for r in routes[1:3]} == {'opencode', 'kilo'}
    assert routes[3].model == 'small-capable' and routes[3].effort == 'low'
    assert [r.cost_class for r in routes[:4]] == ['local','free','free','subscription']


def test_semantics_is_evaluated_independently_of_the_read_tool(tmp_path):
    catalog, project = catalog_fixture(tmp_path)
    spec = evaluation('investigation', [{'id':'read','kind':'tool_evidence','target':'README.md','expected':'read_file'}])
    spec['required_capabilities'] = ['read_file']
    simple, _ = catalog.routes(project, 'molicoder:latest', spec=spec)
    spec['work_type'] = 'analysis'
    reasoning, excluded = catalog.routes(project, 'molicoder:latest', spec=spec)
    assert simple[0].provider == 'ollama'
    assert {r.provider for r in reasoning[:2]} == {'opencode', 'kilo'}
    assert next(r for r in reasoning if r.provider == 'codex').effort == 'medium'
    assert any(e.get('provider') == 'ollama' and 'reasoning evidence' in e['reason'] for e in excluded)


def test_typed_evaluation_requires_cognitive_work_and_legacy_profiles_remain_readable():
    from local_model_chat.orchestration.contracts import EVALUATION_SCHEMA, decode
    spec = evaluation(); del spec['work_type']
    with pytest.raises(ValueError, match='missing required fields'):
        decode(json.dumps(spec), EVALUATION_SCHEMA)
    legacy = dict(spec, schema_version='1.0')
    assert WorkRequirements.from_evaluation(legacy).reasoning == 1


def test_deep_work_uses_sufficient_capacity_and_effort_not_the_cheapest_incapable_route(tmp_path):
    catalog, project = catalog_fixture(tmp_path)
    spec = evaluation('build'); spec.update(complexity='high', required_capabilities=['planning'])
    routes, excluded = catalog.routes(project, 'molicoder:latest', spec=spec)
    assert routes[0].model == 'expensive-first' and routes[0].effort == 'high'
    assert all(r.model != 'small-capable' for r in routes)
    assert any(e.get('provider') == 'kilo' for e in excluded)


def test_legacy_cached_codex_rows_use_current_matching_model_descriptions(tmp_path, monkeypatch):
    catalog, project = catalog_fixture(tmp_path)
    facts = catalog.cached()
    current = facts['cli_models']['codex']
    facts['cli_models']['codex'] = [{k:v for k,v in m.items() if k != 'description'} for m in current]
    catalog.store.set_setting(catalog.KEY, json.dumps(facts))
    monkeypatch.setattr(catalog, 'codex_models', lambda: current)
    routes, _ = catalog.routes(project, 'molicoder:latest', spec=evaluation())
    assert next(r for r in routes if r.provider == 'codex').model == 'small-capable'


def test_untrusted_free_options_are_ranked_before_an_authorized_subscription(tmp_path):
    catalog, project = catalog_fixture(tmp_path, trusted=('codex',))
    routes, _ = catalog.routes(project, 'molicoder:latest', spec=evaluation(), include_untrusted=True)
    assert {r.provider for r in routes[1:3]} == {'opencode','kilo'}
    assert all(r.requires_access for r in routes[1:3])
    assert not routes[3].requires_access


def test_paid_opencode_override_is_not_assumed_free(tmp_path):
    catalog, project = catalog_fixture(tmp_path)
    catalog.store.set_project_preferences(project.id, provider='codex', strategy='HYBRID',
        trusted_providers=('codex','opencode'), model_overrides={'opencode':'vendor/paid'})
    routes, _ = catalog.routes(project, 'molicoder:latest', spec=evaluation())
    route = next(r for r in routes if r.provider == 'opencode')
    assert route.cost_class == 'unknown'
    assert routes.index(route) > next(i for i,r in enumerate(routes) if r.provider == 'codex')


def test_context_capability_and_cooldown_are_not_overridden_by_low_price(tmp_path):
    demand = WorkRequirements(('image_understanding',), 1, False, 5000)
    free = ModelEvidence('free', capacity=2, context_window=4096)
    assert not assess(demand, free).eligible
    assert not assess(replace(demand,input_tokens=0), free).eligible
    vision = replace(free, context_window=16000, capabilities=('image_understanding',))
    assert assess(demand, vision).eligible
    catalog, project = catalog_fixture(tmp_path)
    routes, _ = catalog.routes(project, 'molicoder:latest', spec=evaluation())
    free_route = next(r for r in routes if r.provider == 'opencode')
    catalog.record_outcome(free_route, 'failed', spec=evaluation(), error='429 rate limit')
    routes, excluded = catalog.routes(project, 'molicoder:latest', spec=evaluation())
    assert routes[1].provider == 'kilo'
    assert any(e.get('provider') == 'opencode' and 'cooldown' in e['reason'] for e in excluded)


@pytest.mark.parametrize('consent', [True, False, None])
def test_real_coordinator_requests_free_access_before_using_subscription(tmp_path, monkeypatch, consent):
    coordinator, client, root = service(tmp_path, [evaluation('conversation', []),
        turn('cannot_complete', message='This local model cannot answer')])
    catalog, _ = catalog_fixture(tmp_path / 'catalog', trusted=('codex',))
    coordinator.store.set_setting(catalog.KEY, catalog.store.get_setting(catalog.KEY))
    coordinator.store.set_project_preferences(coordinator.project.id, provider='codex', strategy='HYBRID', trusted_providers=('codex',))
    coordinator.allow_external = True
    asked, used = [], []
    if consent is not None:
        def allow(proposal):
            asked.append(proposal)
            if consent:
                coordinator.store.set_project_preferences(coordinator.project.id, provider='codex', strategy='HYBRID',
                    trusted_providers=('codex', 'opencode', 'kilo'))
            return consent
        coordinator.provider_access = allow
    original = coordinator.invoke
    def invoke(route, messages, schema, purpose):
        if route.provider == 'ollama': return original(route, messages, schema, purpose)
        used.append(route.provider)
        assert route.provider != 'codex', 'Subscription must not precede the suitable free candidates'
        return turn('answer_ready', message='A helpful answer from the free route')
    monkeypatch.setattr(coordinator, 'invoke', invoke)
    answer = coordinator.run('Suggest a friendly project name')
    if consent is True:
        assert answer.receipt['outcome'] == 'completed' and len(used) == 1 and used[0] in {'opencode','kilo'}
    else:
        assert answer.receipt['outcome'] == ('cancelled' if consent is False else 'blocked') and used == []
    if asked: assert set(asked[0]['providers']) == {'opencode','kilo'} and asked[0]['cost_class'] == 'free'


def test_failed_free_model_does_not_skip_the_other_free_provider(tmp_path, monkeypatch):
    coordinator, client, root = service(tmp_path, [evaluation('conversation', []),
        turn('cannot_complete', message='Local unavailable')])
    catalog, _ = catalog_fixture(tmp_path / 'catalog')
    coordinator.store.set_setting(catalog.KEY, catalog.store.get_setting(catalog.KEY))
    coordinator.store.set_project_preferences(coordinator.project.id, provider='codex', strategy='HYBRID',
        trusted_providers=('codex','opencode','kilo'))
    coordinator.allow_external = True
    original = coordinator.invoke; used=[]
    def invoke(route, messages, schema, purpose):
        if route.provider == 'ollama': return original(route,messages,schema,purpose)
        used.append(route.provider)
        assert route.provider != 'codex'
        if len(used) == 1: raise RuntimeError('429 rate limit')
        return turn('answer_ready', message='Kilo answered after the first free model failed')
    monkeypatch.setattr(coordinator, 'invoke', invoke)
    answer = coordinator.run('Explain a naming convention')
    assert answer.receipt['outcome'] == 'completed' and len(used) == 2 and set(used) == {'opencode','kilo'}


def test_changed_free_price_is_rejected_before_native_model_invocation(tmp_path, monkeypatch):
    import time
    from local_model_chat.orchestration.catalog import Route
    from local_model_chat.inference.contracts import InferencePurpose
    coordinator, _, root = service(tmp_path, [])
    coordinator.store.set_project_preferences(coordinator.project.id, provider='opencode', strategy='HYBRID', trusted_providers=('opencode',))
    coordinator.deadline = time.monotonic() + 30
    coordinator.attempts = [{}]
    class Provider:
        def discover_binary(self): return root / 'fixture.exe'
        def analyze(self, *a, **k): pytest.fail('Changed pricing must stop before generation')
    monkeypatch.setattr('local_model_chat.orchestration.coordinator.create_provider', lambda *a, **k: Provider())
    monkeypatch.setattr(CapabilityCatalogService, 'runtime_models', lambda *a, **k: [
        {'model':'vendor/was-free', 'cost':{'input':1, 'output':2}}])
    with pytest.raises(ValueError, match='no inference was sent'):
        coordinator.invoke(Route('opencode','vendor/was-free','opencode',cost_class='free'),
            [{'role':'user','content':'Question'}], {}, InferencePurpose.EXECUTION)


def test_newly_required_provider_can_request_access_without_repeating_an_earlier_choice(tmp_path, monkeypatch):
    from local_model_chat.inference.contracts import InferencePurpose
    coordinator, client, root = service(tmp_path, [RuntimeError('Local runtime is offline')])
    catalog, _ = catalog_fixture(tmp_path / 'catalog', trusted=())
    coordinator.store.set_setting(catalog.KEY, catalog.store.get_setting(catalog.KEY))
    coordinator.store.set_project_preferences(coordinator.project.id, provider='codex', strategy='HYBRID', trusted_providers=())
    coordinator.allow_external = True
    prompts, used = [], []
    def allow(proposal):
        prompts.append(proposal)
        chosen = 'kilo' if proposal['cost_class'] == 'free' else 'codex'
        previous = coordinator.store.get_project_preferences(coordinator.project.id).trusted_providers
        coordinator.store.set_project_preferences(coordinator.project.id, provider='codex', strategy='HYBRID',
            trusted_providers=(*previous, chosen))
        return True
    coordinator.provider_access = allow
    original = coordinator.invoke
    def invoke(route, messages, schema, purpose):
        if route.provider == 'ollama': return original(route, messages, schema, purpose)
        used.append((route.provider, purpose))
        if purpose == InferencePurpose.ANALYSIS:
            spec = evaluation('conversation', []); spec.update(work_type='analysis', complexity='high', required_capabilities=['reasoning'])
            return spec
        return turn('answer_ready', message='The reasoning answer')
    monkeypatch.setattr(coordinator, 'invoke', invoke)
    answer = coordinator.run('Compare several architecture alternatives in detail')
    assert answer.receipt['outcome'] == 'completed'
    assert [p['cost_class'] for p in prompts] == ['free','subscription']
    assert set(prompts[0]['providers']) == {'opencode','kilo'} and prompts[1]['providers'] == ['codex']
    assert used == [('kilo',InferencePurpose.ANALYSIS),('codex',InferencePurpose.EXECUTION)]
