from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox

from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from test_ui import FakeClient


def create_window(qtbot, tmp_path):
    store = ChatStore(tmp_path / 'header.db')
    a = store.upsert_project('Website', str(tmp_path), 'python')
    other_folder = tmp_path / 'mobile'; other_folder.mkdir()
    b = store.upsert_project('Mobile', str(other_folder), 'python')
    first = store.create_conversation('molicoder:latest', 'Navigation plan', project_id=a.id)
    second = store.create_conversation('molicoder:latest', 'Settings work', project_id=a.id)
    other = store.create_conversation('molicoder:latest', 'Mobile chat', project_id=b.id)
    w = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    w.show()
    return w, a, b, first, second, other


def options(combo):
    return {combo.itemData(index): combo.itemText(index) for index in range(combo.count())}


def test_labeled_header_scopes_chats_immediately(qtbot, tmp_path):
    w, a, b, first, second, other = create_window(qtbot, tmp_path)
    general = w.current_conversation_id
    assert w.chat_history_button.text() == 'Chat history'
    assert w.project_selector_label.text() == 'Project'
    assert w.chat_selector_label.text() == 'Chats'
    assert w.chat_switch.currentData() == general
    w.project_switch.setCurrentIndex(w.project_switch.findData(a.id))
    assert options(w.chat_switch) == {first.id: first.title, second.id: second.title}
    assert w.chat_switch.isEnabled()
    assert w.chat_switch.currentData() == w.current_conversation_id == second.id
    w.project_switch.setCurrentIndex(w.project_switch.findData(b.id))
    assert options(w.chat_switch) == {other.id: other.title}
    assert not w.chat_switch.isEnabled()
    w.project_switch.setCurrentIndex(w.project_switch.findData(None))
    assert options(w.chat_switch) == {general: 'New chat'}


def test_chat_dropdown_opens_chat_and_restores_drafts(qtbot, tmp_path):
    w, a, _, first, second, _ = create_window(qtbot, tmp_path)
    w._load_project_list(select_id=a.id)
    w.composer.setPlainText('Settings draft')
    w._set_view('build')
    w.orchestration_worker = SimpleNamespace(isRunning=lambda: False)
    w.chat_switch.setFocus()
    qtbot.keyClick(w.chat_switch, Qt.Key_End)
    assert w.current_conversation_id == first.id
    assert w.view_stack.currentWidget() == w.chat_view
    assert w.composer.toPlainText() == ''
    assert w.orchestration_worker is not None
    w.composer.setPlainText('Navigation draft')
    qtbot.keyClick(w.chat_switch, Qt.Key_Home)
    assert w.current_conversation_id == second.id
    assert w.composer.toPlainText() == 'Settings draft'
    w.orchestration_worker = None


def test_dropdown_tracks_new_rename_delete_and_history_selection(qtbot, tmp_path, monkeypatch):
    w, a, _, first, second, _ = create_window(qtbot, tmp_path)
    w._load_project_list(select_id=a.id)
    w.new_chat_button.click()
    newest = w.current_conversation_id
    assert w.chat_switch.currentData() == newest
    assert set(options(w.chat_switch)) == {first.id, second.id, newest}
    monkeypatch.setattr(QInputDialog, 'getText', lambda *args, **kwargs: ('Renamed chat', True))
    w._rename_chat()
    assert w.chat_switch.currentText() == 'Renamed chat'
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Yes)
    w._delete_chat()
    assert newest not in options(w.chat_switch)
    assert w.chat_switch.currentData() == w.current_conversation_id
    w._load_conversation_list(select_id=first.id)
    assert w.chat_switch.currentData() == first.id
    w.chat_history_button.click()
    assert w.history_dialog.isVisible()
    assert w.history_dialog.windowTitle().startswith('Chat history')
    w.history_dialog.close()
