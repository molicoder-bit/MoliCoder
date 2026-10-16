import json

from local_model_chat.chat_context import CHAT_HISTORY_INSTRUCTION, build_chat_messages
from local_model_chat.config import MODEL_PRESETS
from local_model_chat.context import estimate_messages
from local_model_chat.domain import GenerationMetrics, Message
from local_model_chat.ollama import OllamaClient
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from test_ui import FakeClient


def test_complete_chat_context_preserves_order_and_excludes_reasoning():
    history = [Message(1, 4, 'user', 'Remember Cedar Compass'),
               Message(2, 4, 'assistant', 'Understood', thinking='private reasoning'),
               Message(3, 4, 'system', 'not a conversational turn')]
    messages = build_chat_messages(history, 'Preset behavior', pending_text='What was its name?')
    assert messages[0]['content'].startswith('Preset behavior\n\n' + CHAT_HISTORY_INSTRUCTION)
    assert messages[1:] == [
        {'role': 'user', 'content': 'Remember Cedar Compass'},
        {'role': 'assistant', 'content': 'Understood'},
        {'role': 'user', 'content': 'What was its name?'},
    ]
    assert 'private reasoning' not in json.dumps(messages)


def test_reopened_chat_sends_all_its_turns_through_the_gateway(qtbot, tmp_path, monkeypatch):
    class RecordingClient(FakeClient):
        def stream_chat(self, model, messages, **options):
            self.sent = (model, messages, options)
            yield {'message': {'content': 'Cedar Compass'}}
            return GenerationMetrics(prompt_tokens=120, output_tokens=3, done_reason='stop')

    store = ChatStore(tmp_path / 'reopen.db')
    chat = store.create_conversation('molicoder:latest', 'Existing chat')
    turn_ids = [store.add_message(Message(None, chat.id, role, content)) for role, content in (
        ('user', 'My project is Cedar Compass.'),
        ('assistant', 'I cannot remember earlier messages.'),
        ('user', 'I mean this same chat.'),
        ('assistant', 'Each session starts fresh.'),
    )]
    other = store.create_conversation('molicoder:latest', 'Another chat')
    store.add_message(Message(None, other.id, 'user', 'Other-chat secret sentinel'))
    client = RecordingClient()
    w = MainWindow(ChatStore(store.path), client, start_ollama_check=False)
    qtbot.addWidget(w)
    w._ollama_ready(True, client.tags())
    w._load_conversation_list(select_id=chat.id)
    monkeypatch.setattr('local_model_chat.ui.main_window.ChatWorker.start', lambda _: None)
    w.composer.setPlainText('What name did I tell you?')
    w._send_message()
    worker = w.chat_worker
    planned_messages = list(worker.messages)
    expected_ids = turn_ids + [store.list_messages(chat.id)[-1].id]
    assert 'Other-chat secret sentinel' not in json.dumps(planned_messages)
    assert len(planned_messages) == 6  # system, four saved turns, current question
    assert planned_messages[1]['content'] == 'My project is Cedar Compass.'
    worker.run()  # exercise policy, adapter and response persistence
    assert client.sent[1] == planned_messages
    reply = store.list_messages(chat.id)[-1]
    receipt = store.message_receipt(reply.id)
    assert receipt['turn_ids'] == expected_ids
    assert receipt['conversation_id'] == chat.id
    assert receipt['estimated_prompt_tokens'] == estimate_messages(client.sent[1])
    assert receipt['history_policy'] == 'complete_current_conversation'


def test_safe_limit_includes_system_and_never_drops_older_turns(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from dataclasses import replace
    w = MainWindow(ChatStore(tmp_path / 'limit.db'), FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    monkeypatch.setattr('local_model_chat.ui.main_window.ChatWorker.start', lambda _: None)
    monkeypatch.setitem(MODEL_PRESETS, 'molicoder:latest', replace(MODEL_PRESETS['molicoder:latest'], context_window=128))
    w.output_spin.setValue(32)
    w.composer.setPlainText('Hello')
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args))
    w._send_message()
    assert warnings and w.chat_worker is None
    assert not w.store.list_messages(w.current_conversation_id)
    assert w.composer.toPlainText() == 'Hello'


def test_ollama_stream_transport_contains_the_complete_message_array(monkeypatch):
    from io import BytesIO
    sent = {}
    def respond(request, **kwargs):
        sent.update(json.loads(request.data))
        return BytesIO(b'{"message":{"content":"Cedar Compass"},"done":true}\n')
    monkeypatch.setattr('local_model_chat.ollama.urllib.request.urlopen', respond)
    messages = build_chat_messages([Message(1, 1, 'user', 'Cedar Compass')], 'Assistant', pending_text='Recall the name')
    list(OllamaClient().stream_chat('molicoder:latest', messages, context_window=8192,
         max_output_tokens=100, temperature=0.2, top_p=0.9, show_thinking=False))
    assert sent['messages'] == messages
