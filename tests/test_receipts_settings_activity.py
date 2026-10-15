import sqlite3
from types import SimpleNamespace

from local_model_chat.domain import Message
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.workspace import refresh_workspace, refresh_activity, show_settings


def test_receipt_upgrade_preserves_legacy_and_cascades_deleted_messages(tmp_path):
    path = tmp_path/'legacy.db'
    store = ChatStore(path)
    chat = store.create_conversation('alias')
    message_id = store.add_message(Message(None, chat.id, 'assistant', 'Legacy answer'))
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE message_receipts')
        connection.execute('PRAGMA user_version=14')
    reopened = ChatStore(path)
    assert reopened.list_messages(chat.id)[0].content == 'Legacy answer'
    assert reopened.message_receipt(message_id) is None
    assert list(tmp_path.glob('legacy.db.pre-v15-*.bak'))
    reopened.set_message_receipt(message_id, {'provider':'fixture', 'requested_model':'previous-model'})
    assert reopened.message_receipt(message_id)['requested_model'] == 'previous-model'
    reopened.delete_last_exchange(chat.id)
    assert reopened.message_receipt(message_id) is None


def test_settings_model_page_and_per_reply_identity(qtbot, tmp_path):
    store = ChatStore(tmp_path/'ui.db')
    w = MainWindow(store, start_ollama_check=False); qtbot.addWidget(w); w.show()
    message_id = store.add_message(Message(None, w.current_conversation_id, 'assistant', 'Known answer'))
    store.set_message_receipt(message_id, {'provider':'fixture-api', 'requested_model':'previous-model', 'turn_ids':[1,2], 'attachments':['example.txt']})
    refresh_workspace(w)
    assert 'previous-model' in w.route_identity.text()
    assert 'Next reply: Ollama' in w.route_identity.text()
    assert '2 conversation turns' in w.context_summary.text()
    assert w.model_combo.isHidden()
    show_settings(w, 'Models & providers')
    assert w.model_combo.isVisible()
    assert w._settings_nav.currentRow() == 1
    w._settings_nav.setCurrentRow(0)
    assert w._settings_dialog.isVisible()
    w._settings_dialog.close()


def test_activity_is_visible_across_modes_and_stops_at_failure(qtbot,tmp_path):
    w=MainWindow(ChatStore(tmp_path/'activity.db'),start_ollama_check=False)
    qtbot.addWidget(w);w.show()
    w.orchestration_worker=SimpleNamespace(paused=False,isRunning=lambda:False)
    refresh_activity(w)
    assert w.busy_indicator.timer.isActive()
    assert w.return_to_build.isVisible()
    w.store.set_setting('ui.reduce_motion','true')
    refresh_activity(w)
    assert not w.busy_indicator.timer.isActive()
    assert 'elapsed' in w.current_activity.text()
    w._build_failed('Fixture failure')
    assert not w.busy_indicator.timer.isActive()
    assert not w.work_feedback.isHidden()
    w.orchestration_worker=None


def test_provider_cards_report_readiness_without_sending_inference(qtbot,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QPushButton, QLabel
    from local_model_chat.molicoder.providers import ProviderStatus
    monkeypatch.setattr('local_model_chat.molicoder.providers.probe_all_providers',lambda: (
        ProviderStatus('codex',True,True,auth_verified=True),
        ProviderStatus('claude',True,False,auth_verified=True),
        ProviderStatus('copilot',False,False),
    ))
    w=MainWindow(ChatStore(tmp_path/'providers.db'),start_ollama_check=False);qtbot.addWidget(w)
    show_settings(w,'Models & providers')
    check=next(b for b in w._settings_dialog.findChildren(QPushButton) if b.text().startswith('Check agent connections'))
    check.click()
    qtbot.waitUntil(lambda:getattr(w,'_provider_check_worker',None) is None)
    text='\n'.join(label.text() for label in w._settings_dialog.findChildren(QLabel))
    assert 'Sign-in required' in text and 'Not installed' in text and 'Ready' in text
    w._settings_dialog.close()
