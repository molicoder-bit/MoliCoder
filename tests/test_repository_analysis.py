import json
from pathlib import Path

import pytest

from local_model_chat.domain import GenerationMetrics
from local_model_chat.ollama import ChatResponse
from local_model_chat.repository_analysis import RepositoryAnalysis, RepositoryIndex
from local_model_chat.storage import ChatStore


class Client:
    def __init__(self, responses):
        self.responses, self.calls, self.reviews = list(responses), [], []
        self.review_results = []

    def tags(self):
        return [{'name': 'molicoder:latest', 'digest': 'a'}, {'name': 'local-reasoning:latest', 'digest': 'b'},
                {'name': 'local-coding:latest', 'digest': 'a'}]

    def chat(self, model, messages, **kwargs):
        if messages[0]['content'].startswith('Review whether a code answer'):
            self.reviews.append((model, messages, kwargs))
            return ChatResponse({'content': json.dumps(self.review_results.pop(0) if self.review_results else
                {'supported': True, 'reason': 'The cited function implements this behavior.'})}, GenerationMetrics())
        self.calls.append((model, messages, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return ChatResponse(value if isinstance(value, dict) else {'content': value},
                            GenerationMetrics(prompt_tokens=10, output_tokens=5))


def answer(line=2, quote='return "local"'):
    return json.dumps({'status': 'answered', 'answer': 'The choose_runner function selects the local runner.',
                      'citations': [{'path': 'routing.py', 'line': line, 'quote': quote}]})


def fixture(tmp_path, responses):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'routing.py').write_text('def choose_runner():\n    return "local"\n')
    store = ChatStore(tmp_path / 'profile.sqlite3')
    project = store.upsert_project('project', str(root), 'python')
    client = Client(responses)
    return RepositoryAnalysis(store, project, client), client


def test_inability_automatically_falls_back_with_same_history_and_sources(tmp_path):
    service, client = fixture(tmp_path, [json.dumps({'status': 'cannot_answer', 'reason': 'cannot trace this code'}), answer()])
    result = service.answer('Where is the local runner chosen?', [{'role': 'user', 'content': 'Earlier project context'}])
    assert [call[0] for call in client.calls] == ['molicoder:latest', 'local-reasoning:latest']
    assert 'Earlier project context' in json.dumps(client.calls[1][1])
    assert 'return \\"local\\"' not in result.content  # Rendered answer remains readable.
    assert result.receipt['attempts'][0]['status'] == 'failed'
    assert result.receipt['provider'] == 'ollama'
    assert result.receipt['requested_model'] == 'local-reasoning:latest'
    assert 'routing.py:2' in result.content


@pytest.mark.parametrize('first', [answer(999), answer(2, 'return "cloud"'), 'I cannot access your files',
                                 RuntimeError('Connection timed out')])
def test_bad_evidence_refusal_or_transport_failure_triggers_fallback(tmp_path, first):
    service, client = fixture(tmp_path, [first, answer()])
    result = service.answer('Where is the local runner chosen?')
    assert len(client.calls) == 2
    assert result.metrics.done_reason == 'repository_answer'


def test_unique_source_quote_corrects_offset_line_without_extra_model_calls(tmp_path):
    service, client = fixture(tmp_path, [answer(1)])
    result = service.answer('Where is the local runner chosen?')
    assert len(client.calls) == 1
    assert result.receipt['citations'][0]['line'] == 2
    assert result.receipt['citations'][0]['requested_line'] == 1


def test_repeated_quote_is_resolved_only_inside_the_named_function(tmp_path):
    service, _ = fixture(tmp_path, [json.dumps({'status': 'answered',
        'answer': 'The choose_runner function is responsible for selecting the local runner.',
        'citations': [{'source': 'S1', 'quote': 'return "local"'}]})])
    (service.index.root / 'routing.py').write_text('def choose_runner():\n    return "local"\ndef other():\n    return "local"\n')
    # Backticks identify an exact code symbol, not a fuzzy semantic match.
    service.client.responses[0] = service.client.responses[0].replace('The choose_runner function', 'The `choose_runner` function')
    result = service.answer('Where is the local runner chosen?')
    assert result.receipt['citations'][0]['line'] == 2
    with pytest.raises(ValueError, match='uniquely match'):
        service.index.validate_citations([{'path': 'routing.py', 'line': 1, 'quote': 'return "local"'}])


def test_tools_only_allow_project_reads_and_block_writes(tmp_path):
    service, client = fixture(tmp_path, [{'tool_calls': [{'function': {'name': 'write_file', 'arguments':
        {'path': 'routing.py', 'content': 'changed'}}}]}, answer()])
    result = service.answer('Where is the local runner chosen?')
    assert (service.index.root / 'routing.py').read_text().startswith('def choose_runner')
    assert 'Only repository search' in result.receipt['attempts'][0]['error']
    assert all(t['function']['name'] in {'search_code', 'read_file'} for t in client.calls[0][2]['tools'])


def test_changed_source_invalidates_citations_and_sensitive_files_are_omitted(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'routing.py').write_text('def choose_runner():\n    return "local"\n')
    (root / 'secrets.json').write_text('secret')
    (root / '.env').write_text('KEY=value')
    index = RepositoryIndex(root)
    index.load()
    assert set(index.files) == {'routing.py'}
    index.search('choose runner')
    (root / 'routing.py').write_text('changed')
    with pytest.raises(ValueError, match='changed during analysis'):
        index.validate_citations([{'path': 'routing.py', 'line': 2, 'quote': 'return "local"'}])
    with pytest.raises(ValueError):
        index.read('../outside.py')


def test_no_trust_means_no_external_call_and_failure_is_not_success(tmp_path, monkeypatch):
    service, _ = fixture(tmp_path, ['cannot answer', 'cannot answer'])
    monkeypatch.setattr('local_model_chat.repository_analysis.create_provider', lambda *a, **k: pytest.fail('Untrusted provider called'))
    result = service.answer('Where is the local runner chosen?')
    assert result.metrics.done_reason == 'repository_unresolved'
    assert result.receipt['outcome'] == 'unresolved'


def test_stop_prevents_model_calls_and_fallback(tmp_path):
    service, client = fixture(tmp_path, [answer()])
    service.cancelled = lambda: True
    service.index.cancelled = service.cancelled
    with pytest.raises(InterruptedError):
        service.answer('Where is the local runner chosen?')
    assert not client.calls


def test_aliases_of_same_weights_are_not_retried(tmp_path):
    service, client = fixture(tmp_path, [])
    assert service._local_routes() == ['molicoder:latest', 'local-reasoning:latest']


def test_source_line_reference_copies_the_actual_code(tmp_path):
    payload = json.dumps({'status': 'answered', 'answer': 'The choose_runner function selects the local runner.',
                          'reason': '', 'citations': [{'ref': 'S1:L2'}]})
    service, client = fixture(tmp_path, [payload])
    result = service.answer('Where is the local runner chosen?')
    assert result.receipt['citations'][0]['quote'] == 'return "local"'
    assert 'return "local"' in client.calls[0][2]['response_format']['properties']['citations']['items']['oneOf'][0]['properties']['quote']['enum']


def test_unsupported_claim_is_rejected_even_when_quoted_code_exists(tmp_path):
    service, client = fixture(tmp_path, [answer(), answer()])
    client.review_results = [{'supported': False, 'reason': 'A real code quote does not support the stated behavior.'},
                             {'supported': True, 'reason': 'The second answer is supported.'}]
    result = service.answer('Where is the local runner chosen?')
    assert len(client.calls) == 2 and len(client.reviews) == 2
    assert 'Grounding review rejected' in result.receipt['attempts'][0]['error']
    assert result.receipt['attempts'][1]['grounding_review']['supported'] is True


def test_invented_class_name_triggers_fallback(tmp_path):
    bad = json.loads(answer())
    bad['answer'] = 'The choose_runner method belongs to the `Invented` class.'
    service, client = fixture(tmp_path, [json.dumps(bad), answer()])
    result = service.answer('Where is the local runner chosen?')
    assert len(client.calls) == 2
    assert 'class absent' in result.receipt['attempts'][0]['error']


def test_tool_evidence_and_history_survive_model_fallback(tmp_path):
    calls = {'tool_calls': [{'function': {'name': 'read_file', 'arguments': {'path': 'routing.py', 'start': 2}}}]}
    service, client = fixture(tmp_path, [calls, 'cannot answer', answer()])
    result = service.answer('Where is the local runner chosen?', [{'role': 'assistant', 'content': 'Earlier explanation'}])
    assert [c[0] for c in client.calls] == ['molicoder:latest', 'molicoder:latest', 'local-reasoning:latest']
    second_route = client.calls[-1][1]
    assert 'Earlier explanation' in json.dumps(second_route)
    assert 'SOURCE S2' in second_route[-1]['content']
    assert result.receipt['trace'][1]['tool'] == 'read_file'


def test_an_unexposed_source_line_is_rejected(tmp_path):
    service, client = fixture(tmp_path, [json.dumps({'status': 'answered', 'answer': 'The local runner is selected here.',
        'citations': [{'ref': 'S1:L99'}]}), answer()])
    result = service.answer('Where is the local runner chosen?')
    assert len(client.calls) == 2
    assert 'not given' in result.receipt['attempts'][0]['error']


@pytest.mark.parametrize('enabled,trusted', [(False, True), (True, False)])
def test_external_setting_and_project_trust_are_both_required(tmp_path, monkeypatch, enabled, trusted):
    service, _ = fixture(tmp_path, ['cannot answer', 'cannot answer'])
    service.store.set_project_preferences(service.project.id, provider='codex', strategy='HYBRID',
        trusted_providers=('codex',) if trusted else ())
    service.store.set_setting(f'repository.external_fallback.{service.project.id}', 'true' if enabled else 'false')
    monkeypatch.setattr('local_model_chat.repository_analysis.create_provider', lambda *a, **k: pytest.fail('External provider called'))
    assert service.answer('Where is the local runner chosen?').metrics.done_reason == 'repository_unresolved'


def test_external_fallback_uses_same_question_and_reports_actual_route(tmp_path, monkeypatch):
    from local_model_chat.molicoder.providers import ProviderResult
    service, _ = fixture(tmp_path, ['cannot answer', 'cannot answer'])
    service.store.set_project_preferences(service.project.id, provider='codex', strategy='HYBRID', trusted_providers=('codex',))
    prompts = []
    class Provider:
        def analyze(self, root, prompt, **kwargs):
            prompts.append((root, prompt, kwargs))
            return ProviderResult('completed', answer(), model='provider-reported-model', model_source='reported')
    monkeypatch.setattr('local_model_chat.repository_analysis.create_provider', lambda *a, **k: Provider())
    result = service.answer('Where is the local runner chosen?', [{'role': 'user', 'content': 'Keep this context'}])
    assert len(prompts) == 1
    assert 'Keep this context' in prompts[0][1] and 'Where is the local runner chosen?' in prompts[0][1]
    assert prompts[0][2]['timeout_seconds'] == 90
    assert result.receipt['provider'] == 'codex'
    assert result.receipt['requested_model'] == 'provider-reported-model'
    assert result.receipt['model_source'] == 'reported'


def test_disabled_local_offering_is_not_reenabled(tmp_path):
    from local_model_chat.inference.adapters.ollama import ensure_local_offering
    from local_model_chat.inference.registry import ProviderRegistry
    from local_model_chat.inference.policy import InferencePolicy
    service, client = fixture(tmp_path, [answer()])
    registry = ProviderRegistry(service.store)
    offering = ensure_local_offering(registry, 'molicoder:latest', context_window=16384, max_output_tokens=2048)
    registry.set_offering_state(offering, status='disabled', actor='user', reason='disabled by user')
    service.policy = InferencePolicy(registry)
    result = service.answer('Where is the local runner chosen?')
    assert registry.get_offering(offering).status == 'disabled'
    assert [c[0] for c in client.calls] == ['local-reasoning:latest']
    assert result.receipt['attempts'][0]['status'] == 'failed'


def _shared_evaluation():
    return {'schema_version': '1.0', 'intent': 'investigation', 'objective': 'Locate runner selection',
        'required_capabilities': ['project_read'], 'complexity': 'low', 'missing_information': [], 'reason': 'Read code',
        'acceptance_checks': [{'id': 'source', 'kind': 'tool_evidence', 'target': 'routing.py', 'expected': 'read_file'}]}


def _shared_turn(messages, *, supported):
    payload = {'schema_version': '1.0', 'status': 'cannot_complete', 'message': 'Cannot answer',
        'tool_calls': [], 'evidence_refs': [], 'unmet_criteria': ['source'], 'failure': 'Cannot trace this code'}
    if supported:
        results = [json.loads(m['content'].split(': ', 1)[1]) for m in messages if m['content'].startswith('HOST TOOL RESULT')]
        if results:
            payload.update(status='answer_ready', message='The choose_runner function at routing.py:2 selects the local runner.',
                evidence_refs=[results[-1]['evidence_id']], unmet_criteria=[], failure=None,
                citations=[{'evidence_id': results[-1]['evidence_id'], 'path': 'routing.py', 'line': 1, 'quote': 'def choose_runner():'}])
        else:
            payload.update(status='needs_tools', message='Read source', unmet_criteria=[], failure=None,
                tool_calls=[{'call_id': 'read', 'name': 'read_file', 'arguments': {'path': 'routing.py', 'start': 1, 'end': 10}}])
    return payload


def _shared_client(client, *, external=False):
    def chat(model, messages, **kwargs):
        if 'addresses_request' in kwargs.get('response_format', {}).get('properties', {}):
            return ChatResponse({'content': json.dumps({'addresses_request':True, 'supported':True,
                'reason':'Scripted grounded answer review'})}, GenerationMetrics())
        value = _shared_evaluation() if messages[0]['content'].startswith('You evaluate requests') else _shared_turn(
            messages, supported=model != 'molicoder:latest' and not external)
        return ChatResponse({'content': json.dumps(value)}, GenerationMetrics(prompt_tokens=10, output_tokens=5))
    client.chat = chat


def test_desktop_worker_fallback_saves_answer_and_actual_model(qtbot, tmp_path):
    from local_model_chat.ui.main_window import MainWindow
    service, client = fixture(tmp_path, ['cannot answer', json.dumps({'status': 'answered',
        'answer': 'The choose_runner function selects the local runner.', 'reason': '', 'citations': [{'ref': 'S1:L2'}]})])
    _shared_client(client)
    service.store.set_setting('orchestration.qualification.v1', json.dumps({'local-reasoning:latest': {
        'enabled_for_fallback': True, 'source': 'test fixture', 'passed': 3}}))
    window = MainWindow(service.store, start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=service.project.id)
    window._new_chat()
    window.client = client
    window._installed_model_names = {'another-model:latest'}
    window.composer.setPlainText('Where is the local runner chosen?')
    assert window.send_button.isEnabled()  # A missing selected model must not block fallback.
    window._send_message()
    qtbot.waitUntil(lambda: window.chat_worker is None, timeout=10000)
    messages = window._current_messages()
    assert len(messages) == 2 and 'routing.py:2' in messages[-1].content
    receipt = service.store.message_receipt(messages[-1].id)
    assert receipt['provider'] == 'ollama'
    assert receipt['requested_model'] == 'local-reasoning:latest'
    assert len(receipt['attempts']) == 2
    assert 'Last reply: ollama · local-reasoning:latest' in window.route_identity.text()


def test_desktop_external_handoff_preserves_receipt_and_shows_unknown_cost(qtbot, tmp_path, monkeypatch):
    from local_model_chat.ui.main_window import MainWindow
    from local_model_chat.ui.workspace import refresh_workspace
    from local_model_chat.molicoder.providers import ProviderResult
    service, client = fixture(tmp_path, ['cannot answer', 'cannot answer'])
    _shared_client(client, external=True)
    service.store.set_setting('orchestration.qualification.v1', json.dumps({'local-reasoning:latest': {
        'enabled_for_fallback': True, 'source': 'test fixture', 'passed': 3}}))
    service.store.set_project_preferences(service.project.id, provider='codex', strategy='HYBRID', trusted_providers=('codex',))
    class Provider:
        def analyze(self, root, prompt, **kwargs):
            import re
            messages = [{'role': role.lower(), 'content': content} for role, content in re.findall(
                r'(SYSTEM|USER|ASSISTANT):\n(.*?)(?=\n\n(?:SYSTEM|USER|ASSISTANT):\n|\n\nRESPONSE JSON SCHEMA:|\Z)', prompt, re.S)]
            return ProviderResult('completed', json.dumps(_shared_turn(messages, supported=True)), model='configured-model', model_source='configured')
    monkeypatch.setattr('local_model_chat.orchestration.coordinator.create_provider', lambda *a, **k: Provider())
    window = MainWindow(service.store, start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=service.project.id)
    window._new_chat()
    window.client = client
    window.composer.setPlainText('Where is the local runner chosen?')
    window._send_message()
    qtbot.waitUntil(lambda: window.chat_worker is None, timeout=10000)
    receipt = service.store.message_receipt(window._current_messages()[-1].id)
    assert receipt['provider'] == 'codex' and receipt['model_source'] == 'configured'
    assert [a['provider'] for a in receipt['attempts']] == ['ollama', 'codex']
    assert receipt['attempts'][0]['status'] == 'evaluated'
    assert 'codex · configured-model (configured)' in window.route_identity.text()
    refresh_workspace(window)
    assert '1 external attempts · CLI cost unknown' in window.usage_summary.text()


def test_desktop_cancel_does_not_save_progress_as_an_answer(qtbot, tmp_path):
    from types import SimpleNamespace
    from local_model_chat.ui.main_window import MainWindow
    from local_model_chat.ui.widgets import ChatBubble
    service, _ = fixture(tmp_path, [])
    window = MainWindow(service.store, start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=service.project.id)
    window._new_chat()
    window.chat_worker = SimpleNamespace(repository_analysis=True, deleteLater=lambda: None)
    window.active_bubble = ChatBubble('assistant', 'Inspecting repository...')
    window._pending_receipt = {}
    window._generation_finished(GenerationMetrics(done_reason='cancelled'))
    message = window._current_messages()[-1]
    assert 'analysis stopped' in message.content and 'Inspecting' not in message.content
    assert service.store.message_receipt(message.id)['outcome'] == 'cancelled'
