import json
import sqlite3

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


def test_orchestration_schema_is_initialized(tmp_path) -> None:
    database = tmp_path / "orchestration.sqlite3"
    ChatStore(database)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {
        "projects", "runs", "plans", "tasks", "task_deps", "worker_runs",
        "validations", "failures", "external_calls", "events",
    } <= tables


def test_external_calls_are_persisted_and_counted(tmp_path) -> None:
    store = ChatStore(tmp_path / "external.sqlite3")
    project = store.upsert_project("sample", str(tmp_path / "sample"), "python")
    run_id = store.create_run(project.id, "feature", "AUTO")

    store.add_external_call(run_id, "codex-cli", "planning", "planning_complexity", 20, 5, 1.25, "completed")

    calls = store.list_external_calls(run_id)
    assert len(calls) == 1
    assert calls[0]["purpose"] == "planning"
    assert calls[0]["est_cost"] is None


def test_legacy_external_call_columns_are_migrated_during_initialization(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE external_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                task_id INTEGER,
                provider TEXT NOT NULL,
                purpose TEXT NOT NULL,
                reason TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                est_cost REAL,
                created_at TEXT NOT NULL
            )"""
        )

    ChatStore(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(external_calls)")}
    assert {"seconds", "status"} <= columns


def test_running_runs_can_be_recovered_as_interrupted(tmp_path) -> None:
    store = ChatStore(tmp_path / "recovery.sqlite3")
    project = store.upsert_project("sample", str(tmp_path / "sample"), "python")
    run_id = store.create_run(project.id, "feature", "LOCAL_ONLY")

    recovered = store.recover_interrupted_runs()

    assert recovered == (run_id,)
    assert store.get_run(run_id)["status"] == "interrupted"
