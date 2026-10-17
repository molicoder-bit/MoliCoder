from pathlib import Path
import pytest

from local_model_chat.file_actions import empty_file_intent, create_empty_file
from local_model_chat.project_queries import project_query, inspect_project, ProjectQuery


def test_empty_file_preserves_existing_data_and_rejects_ambiguous_content(tmp_path):
    intent = empty_file_intent('Can you generate a file called hello.txt?', tmp_path)
    assert intent.target == tmp_path / 'hello.txt'
    create_empty_file(intent.target)
    assert intent.target.read_bytes() == b''
    intent.target.write_text('preserve')
    with pytest.raises(FileExistsError):
        create_empty_file(intent.target)
    assert intent.target.read_text() == 'preserve'
    assert empty_file_intent('Create a file called hello.txt containing a greeting', tmp_path) is None
    assert empty_file_intent('Create a file called ../hello.txt', tmp_path).error
    assert empty_file_intent('Create a file called CON.txt', tmp_path).error


def test_project_evidence_is_fresh_bounded_and_path_specific(tmp_path):
    note = tmp_path / 'notes.txt'
    note.write_text('ORCHID-742')
    query = project_query('Locate notes.txt in this project and explain its contents.')
    assert 'ORCHID-742' in inspect_project(tmp_path, query)[0]
    note.write_text('CHANGED-864')
    assert 'CHANGED-864' in inspect_project(tmp_path, query)[0]
    with pytest.raises(ValueError, match='not found'):
        inspect_project(tmp_path, ProjectQuery('file', 'missing/notes.txt'))
    with pytest.raises(ValueError):
        inspect_project(tmp_path, ProjectQuery('file', '../notes.txt'))
    with pytest.raises(ValueError):
        inspect_project(tmp_path, ProjectQuery('file', '.env'))
    note.write_bytes(b'\x00binary')
    with pytest.raises(ValueError, match='UTF-8'):
        inspect_project(tmp_path, query)


def test_ui_file_read_reaches_model_with_evidence(qtbot, tmp_path, monkeypatch):
    import json
    from local_model_chat.domain import GenerationMetrics
    from local_model_chat.ollama import ChatResponse
    from local_model_chat.storage import ChatStore
    from local_model_chat.ui.main_window import MainWindow
    note = tmp_path / 'notes.txt'
    note.write_text('ORCHID-742')
    store = ChatStore(tmp_path / 'ui.db')
    project = store.upsert_project('fixture', str(tmp_path), 'python')
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    window._new_chat()
    requests = []
    class Client:
        def tags(self): return [{'name': 'molicoder:latest', 'digest': 'test'}]
        def chat(self, model, messages, **kwargs):
            requests.append(messages)
            if 'addresses_request' in kwargs.get('response_format', {}).get('properties', {}):
                return ChatResponse({'content':json.dumps({'addresses_request':True,'supported':True,'reason':'Fixture review'})}, GenerationMetrics())
            if messages[0]['content'].startswith('You evaluate requests'):
                payload = {'schema_version': '1.0', 'intent': 'investigation', 'objective': 'Explain notes.txt',
                    'complexity': 'low', 'required_capabilities': ['read_file'], 'missing_information': [], 'reason': '',
                    'acceptance_checks': [{'id': 'read', 'kind': 'tool_evidence', 'target': 'notes.txt', 'expected': 'read_file'}]}
            else:
                results = [json.loads(m['content'].split(': ', 1)[1]) for m in messages if m['content'].startswith('HOST TOOL RESULT')]
                payload = {'schema_version': '1.0', 'status': 'answer_ready' if results else 'needs_tools',
                    'message': 'The file contains ORCHID-742', 'tool_calls': [] if results else [
                        {'call_id': 'read', 'name': 'read_file', 'arguments': {'path': 'notes.txt', 'start': 1, 'end': 1000}}],
                    'evidence_refs': [results[-1]['evidence_id']] if results else [], 'unmet_criteria': [], 'failure': None}
                if results:
                    payload['citations'] = [{'evidence_id': results[-1]['evidence_id'], 'path': 'notes.txt', 'line': 1, 'quote': 'ORCHID-742'}]
            return ChatResponse({'content': json.dumps(payload)}, GenerationMetrics())
    window.client = Client()
    window.composer.setPlainText('Locate notes.txt and explain its contents.')
    window._send_message()
    qtbot.waitUntil(lambda: window.chat_worker is None, timeout=10000)
    assert any('ORCHID-742' in m['content'] for request in requests for m in request)
    receipt = store.message_receipt(window._current_messages()[-1].id)
    assert receipt['outcome'] == 'completed'
    assert 'ORCHID-742' in window._current_messages()[-1].content
    assert any(e['tool'] == 'read_file' and 'ORCHID-742' in e['result']['text'] for e in receipt['evidence'].values())


def test_local_file_build_never_launches_provider(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from local_model_chat.storage import ChatStore
    from local_model_chat.ui.main_window import MainWindow
    store = ChatStore(tmp_path / 'ui.db')
    project = store.upsert_project('fixture', str(tmp_path), 'python')
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr('local_model_chat.ui.main_window.build_orchestrator', lambda **_: pytest.fail('External planner invoked'))
    window.build_request.setPlainText('Create an empty file called result.txt')
    window._run_build()
    assert (tmp_path / 'result.txt').read_bytes() == b''
    assert store.list_local_actions()[0]['outcome'] == 'created'
    assert not store.list_project_runs(project.id)
    from local_model_chat.orchestration.journal import RequestJournal
    request = RequestJournal.load(store, window._last_local_action['request_id'])
    assert request['state'] == 'completed'
    assert any(event['kind'] == 'evaluation' and event['payload']['intent'] == 'action' for event in request['events'])
    assert any(event['kind'] == 'tool_completed' and event['payload']['tool'] == 'write_file' for event in request['events'])


def test_task_write_scope_prevents_test_rewrites(tmp_path):
    from local_model_chat.molicoder.tools import ToolDispatcher, ToolCall
    (tmp_path / 'source.py').write_text('value=1')
    (tmp_path / 'test_source.py').write_text('assert False')
    tools = ToolDispatcher(tmp_path, validation=lambda: 'validation failed', allowed_write_paths=('source.py',))
    with pytest.raises(PermissionError, match='outside this task scope'):
        tools.dispatch(ToolCall('write_file', {'path': 'test_source.py', 'content': 'assert True'}))
    assert (tmp_path / 'test_source.py').read_text() == 'assert False'
    tools.dispatch(ToolCall('write_file', {'path': 'source.py', 'content': 'value=2'}))
    assert (tmp_path / 'source.py').read_text() == 'value=2'


def test_local_resume_never_enters_external_provider_path(tmp_path):
    from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
    from local_model_chat.storage import ChatStore
    store = ChatStore(tmp_path / 'audit.db')
    project = store.upsert_project('test', str(tmp_path), 'python')
    run = store.create_run(project.id, 'test', 'LOCAL_OFFLINE')
    store.finish_run(run, 'needs_attention')
    # The guard must act before reading a worktree or touching any provider.
    obj = object.__new__(SequentialRunOrchestrator)
    obj.store, obj.project = store, project
    with pytest.raises(ValueError, match='no external agent was launched'):
        obj.resume(run)


def test_local_model_apostrophe_escape_preserves_literal_code():
    from local_model_chat.molicoder.tools import parse_tool_calls, ToolParseError
    import json
    code = "values = text.split(',')\n"
    payload = json.dumps({'name': 'write_file', 'arguments': {'path': 'parser.py', 'content': code}})
    malformed = payload.replace("'", "\\'")
    assert parse_tool_calls({'content': malformed})[0].arguments['content'] == code
    assert parse_tool_calls({'content': payload})[0].arguments['content'] == code
    with pytest.raises(ToolParseError):
        parse_tool_calls({'content': payload.replace('values', r'\zvalues')})
