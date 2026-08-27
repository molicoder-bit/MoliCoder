import json

from local_model_chat.domain import GenerationMetrics, Message
from local_model_chat.storage import ChatStore


def test_conversation_roundtrip_and_exports(tmp_path) -> None:
    store = ChatStore(tmp_path / "chats.sqlite3")
    conversation = store.create_conversation("local-coding:latest")
    store.rename_conversation(conversation.id, "Local test")
    store.add_message(Message(None, conversation.id, "user", "Write Python"))
    metrics = GenerationMetrics(prompt_tokens=12, output_tokens=8, output_tokens_per_second=42.0)
    store.add_message(
        Message(None, conversation.id, "assistant", "```py\nprint('ok')\n```", "checked"),
        metrics,
    )

    messages = store.list_messages(conversation.id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[1].thinking == "checked"

    markdown = tmp_path / "chat.md"
    payload_file = tmp_path / "chat.json"
    store.export_markdown(conversation.id, markdown)
    store.export_json(conversation.id, payload_file)
    assert "<summary>Reasoning</summary>" in markdown.read_text(encoding="utf-8")
    payload = json.loads(payload_file.read_text(encoding="utf-8"))
    assert payload["conversation"]["model"] == "local-coding:latest"
    assert len(payload["messages"]) == 2


def test_delete_last_exchange_and_cascade(tmp_path) -> None:
    store = ChatStore(tmp_path / "chats.sqlite3")
    conversation = store.create_conversation("local-reasoning:latest")
    for role in ("user", "assistant", "user", "assistant"):
        store.add_message(Message(None, conversation.id, role, role))
    store.delete_last_exchange(conversation.id)
    assert len(store.list_messages(conversation.id)) == 2
    store.delete_conversation(conversation.id)
    assert store.list_messages(conversation.id) == []
