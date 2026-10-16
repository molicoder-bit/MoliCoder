import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from local_model_chat.controller import MainController
from local_model_chat.domain import Message
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.theme import apply_theme
from local_model_chat.molicoder.planner.schema import validate_provider_schema, PlanValidationError


def test_actual_provider_schema_is_strict_and_rejects_missing_required_field():
    path = Path(__file__).parents[1] / 'src/local_model_chat/molicoder/planner/plan.schema.json'
    schema = json.loads(path.read_text())
    validate_provider_schema(schema)
    schema['properties']['tasks']['items']['required'].remove('artifact_checks')
    with pytest.raises(PlanValidationError, match='every declared'):
        validate_provider_schema(schema)


def test_directory_followup_is_scoped_to_latest_user_request(tmp_path):
    controller = MainController(ChatStore(tmp_path / 'chat.db'))
    history = [Message(None, 1, 'user', f'List contents of "{tmp_path}"'),
               Message(None, 1, 'assistant', 'I cannot access files.')]
    result = controller.handle('can you do it now?', None, history=history)
    assert result.kind == 'directory_listing_request'
    assert Path(result.evidence) == tmp_path
    history.append(Message(None, 1, 'user', 'Explain Python lists'))
    assert controller.handle('can you do it now?', None, history=history).action_state == 'awaiting_clarification'
    assert controller.handle('can you do it now?', None).action_state == 'awaiting_clarification'


def test_directory_followup_in_chat_runs_real_listing_without_model(qtbot, tmp_path, monkeypatch):
    (tmp_path / 'visible.txt').write_text('sample')
    store = ChatStore(tmp_path / 'ui.db')
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    window._new_chat()
    store.add_message(Message(None, window.current_conversation_id, 'user', f'List contents of "{tmp_path}"'))
    store.add_message(Message(None, window.current_conversation_id, 'assistant', 'I cannot access files.'))
    monkeypatch.setattr('local_model_chat.ui.main_window.ChatWorker.start', lambda *_: pytest.fail('Directory request reached model'))
    window.composer.setPlainText('can you do it now?')
    window._send_message()
    qtbot.waitUntil(lambda: window.chat_worker is None)
    assert 'visible.txt' in store.list_messages(window.current_conversation_id)[-1].content


def test_exact_artifact_criteria_do_not_waive_unrelated_review():
    from local_model_chat.molicoder.planner.schema import PlanTask
    from local_model_chat.molicoder.orchestrator import SequentialRunOrchestrator
    task = PlanTask('file', 'file', 'file', 'low', (),
                    acceptance_criteria=('Artifact checks must pass', 'Looks beautiful'),
                    artifact_checks=({'kind': 'file_equals', 'path': 'hello.txt', 'content': ''},))
    assert SequentialRunOrchestrator._unmet_criteria(task) == ('Looks beautiful',)
    from dataclasses import replace
    assert 'Artifact checks must pass' in SequentialRunOrchestrator._unmet_criteria(replace(task, artifact_checks=()))


def test_nested_codex_error_is_readable():
    from local_model_chat.molicoder.planner.codex_cli import provider_failure
    message = json.dumps({'type': 'error', 'message': json.dumps({'error': {'message': 'Invalid schema: artifact_checks missing'}})})
    assert provider_failure(message, 'Verbose startup diagnostics') == 'Invalid schema: artifact_checks missing'


@pytest.mark.parametrize('theme', ['light', 'matrix'])
def test_blocked_build_has_visible_reason_and_disabled_action_colors(qtbot, tmp_path, theme):
    window = MainWindow(ChatStore(tmp_path / 'ui.db'), start_ollama_check=False)
    qtbot.addWidget(window)
    window.store.set_setting('ui.reduce_motion', 'true')
    apply_theme(window, theme)
    window.show()
    window._set_view('build')
    window.build_request.setPlainText('Create a text file')
    QApplication.processEvents()
    assert window.build_blocker.isVisible()
    assert 'no project selected' in window.build_blocker.text()
    for control in (window.build_run_button, window.build_cancel_button, window.build_pause_button, window.build_provider):
        assert not control.isEnabled()
    # QSS must apply the disabled palette even to buttons with object IDs.
    assert window.build_run_button.palette().buttonText().color() == window.build_pause_button.palette().buttonText().color()
    assert window.build_cancel_button.palette().buttonText().color() == window.build_pause_button.palette().buttonText().color()
    assert window.build_agent_info.maximumHeight() > 48
