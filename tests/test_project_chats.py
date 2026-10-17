import sqlite3

import pytest
from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QMessageBox

from local_model_chat.domain import Attachment, GenerationMetrics, Message
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from test_ui import FakeClient


def project(store, root, name):
    path = root / name
    path.mkdir()
    return store.upsert_project(name, str(path), 'python')


def chat_ids(window):
    return [window.chat_list.item(i).data(Qt.UserRole) for i in range(window.chat_list.count())]


def choose(window, project_id):
    window.project_switch.setCurrentIndex(window.project_switch.findData(project_id))


def test_one_project_has_many_chats_and_general_is_separate(tmp_path):
    store = ChatStore(tmp_path / 'chat.db')
    a = project(store, tmp_path, 'A')
    b = project(store, tmp_path, 'B')
    general = store.create_conversation('local', 'General')
    first = store.create_conversation('local', 'First', project_id=a.id)
    second = store.create_conversation('local', 'Second', project_id=a.id)
    other = store.create_conversation('local', 'Other', project_id=b.id)
    store.add_message(Message(None, first.id, 'user', 'Keep this history'))
    restored = ChatStore(store.path)
    assert {c.id for c in restored.list_project_conversations(a.id)} == {first.id, second.id}
    assert [c.id for c in restored.list_project_conversations(b.id)] == [other.id]
    assert [c.id for c in restored.list_project_conversations(None)] == [general.id]
    assert restored.list_messages(first.id)[0].content == 'Keep this history'
    assert restored.get_conversation_project_id(second.id) == a.id
    with pytest.raises(sqlite3.IntegrityError):
        store.create_conversation('local', project_id=99999)
    assert len(store.list_conversations()) == 4  # failed creation leaves no orphan


def test_project_switch_restores_its_chat_draft_and_attachments(qtbot, tmp_path):
    store = ChatStore(tmp_path / 'ui.db')
    a = project(store, tmp_path, 'A')
    b = project(store, tmp_path, 'B')
    w = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    general = w.current_conversation_id
    w.composer.setPlainText('General draft')
    choose(w, a.id)
    first = w.current_conversation_id
    w.new_chat_button.click()
    second = w.current_conversation_id
    assert set(chat_ids(w)) == {first, second}
    assert 'A chats' in w.chats_scope_label.text()
    attachment = Attachment(tmp_path / 'note.txt', 'note.txt', 4, 'note', 'abc')
    w.composer.setPlainText('A second draft')
    w.current_attachments = [attachment]
    w.build_request.setPlainText('A build draft')
    choose(w, b.id)
    other = w.current_conversation_id
    assert chat_ids(w) == [other]
    assert not w.current_attachments and not w.composer.toPlainText()
    w.composer.setPlainText('B draft')
    choose(w, a.id)
    assert w.current_conversation_id == second
    assert w.composer.toPlainText() == 'A second draft'
    assert w.current_attachments == [attachment]
    assert w.build_request.toPlainText() == 'A build draft'
    w.chat_list.setCurrentRow(chat_ids(w).index(first))
    choose(w, None)
    assert chat_ids(w) == [general]
    assert w.composer.toPlainText() == 'General draft'
    assert not w.remove_project_button.isEnabled()
    assert not w.recycle_project_button.isEnabled()
    w._remove_project()
    w._recycle_project()
    assert len(store.list_projects()) == 2
    choose(w, a.id)
    assert w.current_conversation_id == first
    assert len(store.list_conversations()) == 4  # switching never duplicates chats


def test_restart_and_delete_remain_in_project_scope(qtbot, tmp_path, monkeypatch):
    store = ChatStore(tmp_path / 'restart.db')
    a = project(store, tmp_path, 'A')
    b = project(store, tmp_path, 'B')
    first = store.create_conversation('molicoder:latest', project_id=a.id)
    second = store.create_conversation('molicoder:latest', project_id=a.id)
    other = store.create_conversation('molicoder:latest', project_id=b.id)
    store.set_setting('molicoder.selected_project_id', str(a.id))
    store.set_setting('ui.remember_workspace', 'true')
    w = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    assert w.current_conversation_id == second.id
    assert set(chat_ids(w)) == {first.id, second.id}
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Yes)
    w._delete_chat()
    assert w.current_project_id == a.id and w.current_conversation_id == first.id
    w._delete_chat()
    replacement = w.current_conversation_id
    assert replacement not in {first.id, second.id, other.id}
    assert store.get_conversation_project_id(replacement) == a.id
    assert store.get_conversation(other.id) is not None
    w._load_conversation_list(select_id=other.id)
    assert w.current_project_id == b.id and chat_ids(w) == [other.id]


def test_last_selected_older_chat_survives_restart_and_archiving(qtbot, tmp_path, monkeypatch):
    store = ChatStore(tmp_path / 'selected.db')
    a = project(store, tmp_path, 'A')
    older = store.create_conversation('molicoder:latest', project_id=a.id)
    store.create_conversation('molicoder:latest', project_id=a.id)
    w = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    choose(w, a.id)
    w.chat_list.setCurrentRow(chat_ids(w).index(older.id))
    store.set_setting('ui.remember_workspace', 'true')
    w.close()
    restored = MainWindow(ChatStore(store.path), FakeClient(), start_ollama_check=False)
    qtbot.addWidget(restored)
    assert restored.current_project_id == a.id
    assert restored.current_conversation_id == older.id
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Yes)
    restored.composer.setPlainText('Preserve archived draft')
    restored._remove_project()
    assert restored.current_project_id is None
    assert older.id not in chat_ids(restored)
    assert restored._conversation_drafts[older.id][0] == 'Preserve archived draft'
    restored._load_conversation_list(select_id=older.id)
    assert restored.current_project_id is None
    assert store.get_conversation_project_id(older.id) == a.id
    assert store.get_conversation(older.id) is not None


@pytest.mark.parametrize('repository_mode', [False, True])
def test_switch_cancels_old_stream_and_blocks_cross_project_during_build(qtbot, tmp_path, monkeypatch, repository_mode):
    class Worker(QObject):
        content_delta = Signal(str)
        thinking_delta = Signal(str)
        completed = Signal(object)
        failed = Signal(str)
        finished = Signal()
        route_changed = Signal(object)
        review_required = Signal(object)
        repository_analysis = repository_mode

        def __init__(self, *args):
            super().__init__()
            self.cancelled = False
            self.model = args[1]
            self.provider = 'ollama'
            self.request_id = 'old-request'
        def start(self): pass
        def cancel(self): self.cancelled = True
        def isRunning(self): return False

    monkeypatch.setattr('local_model_chat.ui.main_window.ChatWorker', Worker)
    monkeypatch.setattr('local_model_chat.ui.main_window.RepositoryChatWorker', Worker)
    monkeypatch.setattr('local_model_chat.ui.main_window.RequestWorker', Worker)
    store = ChatStore(tmp_path / 'workers.db')
    a = project(store, tmp_path, 'A')
    b = project(store, tmp_path, 'B')
    w = MainWindow(store, FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    choose(w, a.id)
    w.composer.setPlainText('Where is this project behavior implemented?' if repository_mode else 'Explain Python functions')
    assert w._repository_request(w.composer.toPlainText()) is repository_mode
    w._send_message()
    old = w.chat_worker
    choose(w, b.id)
    current = w.current_conversation_id
    assert old.cancelled and w.chat_worker is None
    old.content_delta.emit('Wrong project')
    old.completed.emit(GenerationMetrics())
    old.finished.emit()
    old.route_changed.emit({'phase': 'started', 'request_id': 'old-request', 'provider': 'codex', 'model': 'wrong-project'})
    assert not store.list_messages(current)
    assert not w._retired_chat_workers
    w.orchestration_worker = object()
    choose(w, a.id)
    assert w.current_project_id == b.id
    assert w.project_switch.currentData() == b.id
    assert chat_ids(w) == [current]
    w.new_chat_button.click()
    assert store.get_conversation_project_id(w.current_conversation_id) == b.id
    w.orchestration_worker = None
