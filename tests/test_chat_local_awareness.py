from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from local_model_chat.context import estimate_messages
from local_model_chat.controller import MainController
from local_model_chat.directory_queries import DirectoryListing, directory_question, directory_target, list_directory
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from test_ui import FakeClient


@pytest.mark.parametrize('text', [
    "good, do you know what project we're working on?", 'Which project is selected?',
    "What project are we working on?", 'Can you tell me what our current project is?',
])
def test_project_identity_uses_records_and_explains_general_chat(tmp_path, text):
    store = ChatStore(tmp_path / 'project.db')
    project = store.upsert_project('Cedar Compass', str(tmp_path), 'python')
    controller = MainController(store)
    result = controller.handle(text, project.id)
    assert result is not None and result.kind == 'project_identity'
    assert 'Cedar Compass' in result.content and str(tmp_path) in result.content
    assert controller.can_handle_locally(text)
    assert 'General chat' in controller.handle(text, None).content


@pytest.mark.parametrize('text', [
    'OK, can you tell me which content is in c:/Dev?', 'List contents of "c:/Dev"',
    "What's in c:/Dev?", 'show me c:/Dev', 'dir c:/Dev',
    "Ok that's good, can you tell me the contents of the folder c:/Dev?",
    'Thanks, could you show me what is inside the directory "c:/Dev"?',
    'Can you give me a list of all the files in c:/Dev please?',
    'Can you tell me the contents of c:/Dev?',
])
def test_reported_folder_question_variants(text):
    assert directory_question(text).target.lower() == 'c:/dev'


def test_folder_paths_relative_clarification_and_unrelated_chat(tmp_path):
    store = ChatStore(tmp_path / 'paths.db')
    project = store.upsert_project('Example', str(tmp_path), 'python')
    assert directory_target(directory_question('List files in src'), project) == tmp_path / 'src'
    assert directory_target(directory_question('List my project folder'), project) == tmp_path
    result = MainController(store).handle('List files in src', None)
    assert result.action_state == 'awaiting_clarification'
    for text in ('list ideas', "what's in a name", 'Explain how to list directory entries', 'delete C:/Dev',
                 'List ideas about C:/Dev', "Don't list contents of C:/Dev", 'Explain how to list contents of C:/Dev'):
        assert directory_question(text) is None
    with pytest.raises(ValueError, match='network'):
        directory_target(directory_question('List contents of "//server/share"'), project)


def test_listing_is_read_only_bounded_and_does_not_open_file_contents(tmp_path, monkeypatch):
    root = tmp_path / 'folder';root.mkdir()
    (root / 'README.md').write_text('NEVER READ THIS FILE CONTENT', encoding='utf-8')
    (root / 'nested').mkdir()
    (root / 'nested' / 'deep.txt').write_text('nested secret', encoding='utf-8')
    monkeypatch.setattr(Path, 'read_text', lambda *args, **kwargs: pytest.fail('Read file contents'))
    result = list_directory(root)
    assert result.outcome == 'completed' and result.entries == 2
    assert 'README.md' in result.content and 'Folder:' in result.content
    assert 'deep.txt' not in result.content and 'NEVER READ' not in result.content
    assert not result.truncated and result.checked_at
    limited = list_directory(root, max_entries=1)
    assert limited.entries == 1 and limited.truncated
    assert 'More entries may exist' in limited.content
    stopped = list_directory(root, max_seconds=0)
    assert stopped.truncated and 'empty' not in stopped.content
    assert list_directory(root, cancelled=lambda: True).outcome == 'cancelled'


def test_empty_missing_file_and_permission_results_are_truthful(tmp_path, monkeypatch):
    root = tmp_path / 'empty';root.mkdir()
    assert 'empty' in list_directory(root).content
    assert list_directory(root / 'missing').outcome == 'failed'
    file = root / 'file.txt';file.write_text('x')
    assert 'not a folder' in list_directory(file).content
    def deny(_): raise PermissionError('fixture')
    monkeypatch.setattr('local_model_chat.directory_queries.os.scandir', deny)
    assert 'Access denied' in list_directory(root).content


def test_filename_markup_is_literal_and_cannot_become_a_link(tmp_path):
    from PySide6.QtGui import QTextDocument
    name = '`[title](url).txt'
    (tmp_path / name).write_text('Do not open')
    listing = list_directory(tmp_path)
    document = QTextDocument()
    document.setMarkdown(listing.content)
    assert name in document.toPlainText()
    assert 'href="url"' not in document.toHtml()


def test_project_metadata_follows_conversation_instead_of_old_model_guess(qtbot, tmp_path, monkeypatch):
    w = MainWindow(ChatStore(tmp_path / 'scope.db'), FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    a = w.store.upsert_project('Project Alpha', str(tmp_path), 'python')
    bpath = tmp_path / 'b';bpath.mkdir()
    b = w.store.upsert_project('Project Beta', str(bpath), 'node')
    w._load_project_list(select_id=a.id)
    monkeypatch.setattr('local_model_chat.ui.main_window.ChatWorker.start', lambda _: None)
    w.composer.setPlainText('Explain a helpful next step')
    w._send_message()
    worker = w.chat_worker
    assert 'Project Alpha' in worker.messages[0]['content']
    assert 'Project Beta' not in worker.messages[0]['content']
    assert w._pending_receipt['project_metadata']['id'] == a.id
    assert w._pending_receipt['estimated_prompt_tokens'] == estimate_messages(worker.messages)
    w._reset_generation_state()
    w._load_project_list(select_id=b.id)
    w.composer.setPlainText("good, do you know what project we're working on?")
    w._send_message()
    reply = w.store.list_messages(w.current_conversation_id)[-1]
    assert 'Project Beta' in reply.content and 'Project Alpha' not in reply.content
    assert w.store.message_receipt(reply.id)['model_source'] == 'no_inference'


@pytest.mark.parametrize('user_message', [
    'OK, can you tell me which content is in "{path}"?',
    "Ok that's good, can you tell me the contents of the folder \"{path}\"?",
    'Thanks, could you show me what is inside the directory "{path}"?',
])
def test_folder_read_uses_background_worker_and_persists_no_model_receipt(qtbot, tmp_path, monkeypatch, user_message):
    root = tmp_path / 'listing';root.mkdir()
    (root / 'hello.txt').write_text('secret file contents')
    w = MainWindow(ChatStore(tmp_path / 'listing.db'), FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    w.show()
    def forbid_model(*args, **kwargs):
        pytest.fail('A folder listing must not fall through to model inference')
    monkeypatch.setattr('local_model_chat.ui.main_window.ChatWorker', forbid_model)
    w.composer.setPlainText(user_message.format(path=root))
    w._send_message()
    qtbot.waitUntil(lambda: w.chat_worker is None)
    reply = w.store.list_messages(w.current_conversation_id)[-1]
    assert 'hello.txt' in reply.content and 'secret file contents' not in reply.content
    receipt = w.store.message_receipt(reply.id)
    assert receipt['model_source'] == 'no_inference'
    assert receipt['entries'] == 1 and receipt['outcome'] == 'completed'
    assert receipt['checked_at'] and not receipt['truncated']
    assert 'No AI model used' in w.route_identity.text()
    assert not w.busy_indicator.timer.isActive()


def test_late_local_read_cannot_write_into_new_chat(qtbot, tmp_path, monkeypatch):
    class FakeRead(QObject):
        completed = Signal(object)
        finished = Signal()
        local_read = True
        def __init__(self, path):
            super().__init__();self.path=path;self._cancelled=False
        def start(self): pass
        def cancel(self): self._cancelled=True
        def isRunning(self): return False
    monkeypatch.setattr('local_model_chat.ui.main_window.DirectoryReadWorker', FakeRead)
    w = MainWindow(ChatStore(tmp_path / 'late.db'), FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    w.composer.setPlainText(f'List contents of "{tmp_path}"')
    w._send_message()
    old = w.chat_worker
    assert 'Reading local folder' in w.route_identity.text()
    w.new_chat_button.click()
    old.completed.emit(DirectoryListing('wrong chat', str(tmp_path), 'completed'))
    old.finished.emit()
    assert old._cancelled and not w.store.list_messages(w.current_conversation_id)
    assert not w._retired_chat_workers


def test_packaged_smoke_journey_uses_the_reopened_chat_and_real_reader(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from local_model_chat.domain import Message
    from local_model_chat.ui.chat_smoke import verify_local_chat
    store = ChatStore(tmp_path / 'smoke.db')
    old = store.create_conversation('molicoder:latest', 'Previously saved chat')
    store.add_message(Message(None, old.id, 'user', 'Saved user history'))
    store.add_message(Message(None, old.id, 'assistant', 'I cannot browse directories.'))
    project = store.upsert_project('Fixture project', str(tmp_path), 'python')
    w = MainWindow(ChatStore(store.path), FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    def forbid_model(*args, **kwargs):
        pytest.fail('Packaged Chat journey attempted model inference')
    monkeypatch.setattr('local_model_chat.ui.main_window.ChatWorker', forbid_model)
    verify_local_chat(w, project.id, old.id, tmp_path, QApplication.instance())
    assert store.list_messages(old.id)[0].content == 'Saved user history'
