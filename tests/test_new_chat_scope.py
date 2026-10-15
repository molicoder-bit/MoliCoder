from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow


def test_default_start_is_general_and_new_chat_is_one_click(qtbot, tmp_path):
    store = ChatStore(tmp_path/'chat.db')
    project = store.upsert_project('neworld', str(tmp_path), 'python')
    store.set_setting('molicoder.selected_project_id', str(project.id))
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    window.show()
    assert window.current_project_id is None
    assert window.registry.selected() is None
    old = window.current_conversation_id
    window.composer.setPlainText('keep my draft')
    window._set_view('build')
    assert window.new_chat_button.isVisible()
    window.new_chat_button.click()
    assert window.current_conversation_id != old
    assert window.view_stack.currentWidget() == window.chat_view
    assert window.composer.toPlainText() == ''
    assert window._conversation_drafts[old][0] == 'keep my draft'


def test_explicit_project_restore_and_deselection(qtbot, tmp_path):
    store = ChatStore(tmp_path/'restore.db')
    project = store.upsert_project('neworld', str(tmp_path), 'python')
    store.set_setting('molicoder.selected_project_id', str(project.id))
    store.set_setting('ui.remember_workspace', 'true')
    window = MainWindow(store, start_ollama_check=False)
    qtbot.addWidget(window)
    assert window.current_project_id == project.id
    window.project_switch.setCurrentIndex(window.project_switch.findData(None))
    assert window.registry.selected() is None
    assert window.build_request.isEnabled()


def test_new_chat_rejects_late_tokens_from_previous_reply(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from test_ui import FakeClient
    from local_model_chat.domain import GenerationMetrics

    class FakeWorker(QObject):
        content_delta = Signal(str)
        thinking_delta = Signal(str)
        completed = Signal(object)
        failed = Signal(str)
        finished = Signal()

        def __init__(self, client, model, *args):
            super().__init__(); self.model=model; self.cancelled=False
        def start(self): pass
        def cancel(self): self.cancelled=True
        def isRunning(self): return False

    monkeypatch.setattr('local_model_chat.ui.main_window.ChatWorker', FakeWorker)
    window=MainWindow(ChatStore(tmp_path/'late.db'),FakeClient(),start_ollama_check=False)
    qtbot.addWidget(window)
    window.composer.setPlainText('Hello model')
    window._send_message()
    old=window.chat_worker
    assert old is not None
    window.new_chat_button.click()
    current=window.current_conversation_id
    old.content_delta.emit('This must never appear in the new conversation')
    old.completed.emit(GenerationMetrics())
    old.finished.emit()
    assert old.cancelled
    assert not window.store.list_messages(current)
    assert not window._retired_chat_workers


def test_missing_selected_model_explains_disabled_send(qtbot,tmp_path):
    from test_ui import FakeClient
    w=MainWindow(ChatStore(tmp_path/'missing-model.db'),FakeClient(),start_ollama_check=False)
    qtbot.addWidget(w)
    w._ollama_ready(True,[{'name':'another-installed-model'}])
    w.composer.setPlainText('Hello model')
    assert not w.send_button.isEnabled()
    assert 'not installed' in w.composer_hint.text()
    w.composer.setPlainText(f'create a folder called "one" in {tmp_path}')
    assert w.send_button.isEnabled()


def test_model_preference_survives_restart_and_missing_runtime_model(qtbot,tmp_path):
    from test_ui import FakeClient
    store=ChatStore(tmp_path/'preference.db')
    w=MainWindow(store,FakeClient(),start_ollama_check=False);qtbot.addWidget(w)
    w._ollama_ready(True,[{'name':'molicoder:latest'},{'name':'my-chosen-model'}])
    w.model_combo.setCurrentIndex(w.model_combo.findData('my-chosen-model'))
    assert store.get_setting('ui.chat_model') == 'my-chosen-model'
    w.close()
    restored=MainWindow(store,FakeClient(),start_ollama_check=False);qtbot.addWidget(restored)
    assert restored.preset.name == 'my-chosen-model'
    restored._ollama_ready(True,[{'name':'another-installed-model'}])
    assert restored.preset.name == 'my-chosen-model'
    restored.composer.setPlainText('Hello')
    assert not restored.send_button.isEnabled() and 'not installed' in restored.composer_hint.text()
