import json
import os

import pytest

from local_model_chat.orchestration.journal import RequestJournal
from local_model_chat.orchestration.tools import ToolSession
from local_model_chat.orchestration.coordinator import RequestCoordinator
from test_request_orchestration import Client, evaluation, write_call
from local_model_chat.storage import ChatStore


def interrupted(tmp_path, *, command=False):
    root = tmp_path / 'project'; root.mkdir()
    store = ChatStore(tmp_path / 'profile.db')
    project = store.upsert_project('Fixture', str(root), 'generic')
    chat = store.create_conversation('molicoder:latest', project_id=project.id)
    journal = RequestJournal(store, 'preserved')
    journal.start('Create Iworked.txt with yeay', project, [], conversation_id=chat.id)
    journal.transition('evaluating'); journal.event('evaluation', evaluation()); journal.transition('running')
    tools = ToolSession(root, journal, cancelled=lambda: False, review=lambda _: True, emit=lambda *a: None)
    if command:
        journal.event('tool_started', {'call': {'call_id': 'command-1', 'name': 'run_command', 'arguments': {'argv': ['unknown']}}})
    else:
        tools.dispatch(write_call(), evaluation())
    with store._connect() as db:
        spec = json.loads(db.execute('SELECT spec_json FROM orchestration_requests').fetchone()[0])
        spec['owner'] = {'pid': 999999999, 'birth': 'gone'}
        db.execute('UPDATE orchestration_requests SET spec_json=?', (json.dumps(spec),)); db.commit()
    return store, project, chat


def test_restart_reconciles_completed_write_without_inference_or_duplicate_review(tmp_path):
    store, project, _ = interrupted(tmp_path)
    client = Client([])
    result = RequestCoordinator(store, project, client, resume_from='preserved', allow_external=False,
        review=lambda _: pytest.fail('Completed write must not be repeated')).run('')
    assert result.receipt['outcome'] == 'completed'
    assert (tmp_path / 'project/Iworked.txt').read_bytes() == b'yeay'
    assert client.calls == []
    assert RequestJournal.load(store, 'preserved')['state'] == 'resumed'
    with pytest.raises(ValueError, match='cannot resume'):
        RequestCoordinator(store, project, client, resume_from='preserved').run('')


def test_changed_file_or_uncertain_command_blocks_replay(tmp_path):
    store, project, _ = interrupted(tmp_path)
    (tmp_path / 'project/Iworked.txt').write_text('user edited')
    result = RequestCoordinator(store, project, Client([]), resume_from='preserved').run('')
    assert result.receipt['outcome'] == 'blocked'
    assert 'changed since interruption' in result.content
    other = tmp_path / 'command'; other.mkdir()
    store, project, _ = interrupted(other, command=True)
    result = RequestCoordinator(store, project, Client([]), resume_from='preserved').run('')
    assert result.receipt['outcome'] == 'blocked'
    assert 'without a recorded result' in result.content


def test_live_owner_is_never_recovered_and_deleted_chat_removes_request_copy(tmp_path):
    store = ChatStore(tmp_path / 'profile.db')
    chat = store.create_conversation('molicoder:latest')
    journal = RequestJournal(store, 'active')
    journal.start('private chat text', None, [], conversation_id=chat.id)
    journal.event('inference', {'content': 'private recorded output'})
    assert RequestJournal.recover_interrupted(store) == []
    assert RequestJournal.load(store, 'active')['state'] == 'received'
    store.delete_conversation(chat.id)
    with pytest.raises(ValueError, match='does not exist'):
        RequestJournal.load(store, 'active')


def test_explicit_build_uses_shared_typed_intake_and_is_not_mislabeled_complete(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    store = ChatStore(tmp_path / 'profile.db')
    project = store.upsert_project('Fixture', str(root), 'generic')
    run = store.create_run(project.id, 'Implement feature', 'HYBRID', provider='codex')
    key = RequestCoordinator.register_build(store, project, run, 'Implement feature', 'HYBRID', provider='codex')
    assert RequestCoordinator.register_build(store, project, run, 'Implement feature', 'HYBRID') == key
    saved = RequestJournal.load(store, key)
    assert saved['state'] == 'delegated' and saved['spec']['build_run_id'] == run
    assert any(e['kind'] == 'evaluation' and e['payload']['intent'] == 'build' for e in saved['events'])
    store.clear_all()
    assert RequestJournal.load(store, key)['state'] == 'delegated'
    store.clear_project_history(project.id)
    with pytest.raises(ValueError, match='does not exist'):
        RequestJournal.load(store, key)


def test_host_command_records_real_cwd_exit_and_failure_without_replay(tmp_path):
    import sys
    store = ChatStore(tmp_path / 'profile.db')
    journal = RequestJournal(store, 'command'); journal.start('Run a command', None, [])
    tools = ToolSession(tmp_path, journal, cancelled=lambda: False, review=lambda _: True, emit=lambda *a: None)
    spec = evaluation(checks=[{'id': 'process', 'kind': 'tool_evidence', 'target': '.', 'expected': 'run_command'}])
    call = {'call_id': 'pwd', 'name': 'run_command', 'arguments': {'argv': [sys.executable, '-c', 'import os; print(os.getcwd())']}}
    result = tools.dispatch(call, spec)
    assert result['result']['exit_code'] == 0
    assert str(tmp_path) in result['result']['output']
    assert tools.dispatch(call, spec) == result
    with pytest.raises(PermissionError, match='inspect its recorded outcome'):
        tools.dispatch({'call_id': 'bad', 'name': 'run_command', 'arguments': {'argv': [sys.executable, '-c', 'raise SystemExit(7)']}}, spec)
    events = RequestJournal.load(store, 'command')['events']
    assert [e['payload']['exit_code'] for e in events if e['kind'] == 'command_result'] == [0, 7]


def test_route_cooldown_persists_and_does_not_change_other_models(tmp_path):
    from local_model_chat.orchestration.catalog import CapabilityCatalogService
    store = ChatStore(tmp_path / 'profile.db')
    catalog = CapabilityCatalogService(store, Client([])); catalog.refresh(discover_runtimes=False)
    store.set_setting('orchestration.qualification.v1', json.dumps({'local-coding:latest': {'enabled_for_fallback': True}}))
    routes, _ = catalog.routes(None, 'molicoder:latest', allow_external=False)
    catalog.record_outcome(routes[0], 'failed', error='service unavailable 503')
    after, excluded = CapabilityCatalogService(store, Client([])).routes(None, 'molicoder:latest', allow_external=False)
    assert len(after) == 1 and after[0].model == 'local-coding:latest'
    assert any(e['reason'] == 'Temporary failure cooldown' for e in excluded)


def test_source_quotes_and_newline_bytes_are_checked_against_current_evidence(tmp_path):
    store = ChatStore(tmp_path / 'profile.db')
    journal = RequestJournal(store, 'read'); journal.start('Inspect source', None, [])
    (tmp_path / 'routing.py').write_bytes(b'def choose_runner():\r\n    return "local"\r\n')
    tools = ToolSession(tmp_path, journal, cancelled=lambda: False, review=lambda _: True, emit=lambda *a: None)
    read = tools.dispatch({'call_id': 'source', 'name': 'read_file', 'arguments': {'path': 'routing.py', 'start': 1, 'end': 10}}, evaluation('investigation'))
    assert read['result']['raw_excerpt'].endswith('\r\n')
    assert read['result']['newline_style'] == 'CRLF'
    cite = {'evidence_id': read['evidence_id'], 'path': 'routing.py', 'line': 1, 'quote': 'def choose_runner():'}
    assert tools.verify_citations([cite], 'The `choose_runner` function is defined here')[0]['line'] == 1
    with pytest.raises(ValueError):
        tools.verify_citations([dict(cite, quote='def nonexistent():')], 'Fake source')
    (tmp_path / 'routing.py').write_bytes(b'def changed():\r\n    return "local"\r\n')
    with pytest.raises(ValueError):
        tools.verify_citations([cite], 'Stale source')
