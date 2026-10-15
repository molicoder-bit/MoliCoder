import json
import shutil
import sqlite3
import pytest

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


def test_knowledge_schema_upgrade_preserves_data_and_online_backup(tmp_path) -> None:
    database = tmp_path / "legacy-chat.sqlite3"
    old = ChatStore(database)
    conversation = old.create_conversation("local-test")
    old.add_message(Message(None, conversation.id, "user", "Retain this"))
    project = old.upsert_project("Sample", str(tmp_path / "sample"), "python")
    run_id = old.create_run(project.id, "Feature", "LOCAL_ONLY")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 2")
        for table in ("decision_records", "knowledge_evidence", "knowledge_revisions", "knowledge_items"):
            connection.execute(f"DROP TABLE {table}")

    upgraded = ChatStore(database)
    backups = list(tmp_path.glob("legacy-chat.sqlite3.pre-v14-*.bak"))

    assert len(backups) == 1
    assert upgraded.list_messages(conversation.id)[0].content == "Retain this"
    assert upgraded.get_run(run_id)["request"] == "Feature"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == ChatStore.SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"knowledge_items", "knowledge_revisions", "knowledge_evidence", "decision_records", "conversation_context", "knowledge_links"} <= tables
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT content FROM messages WHERE id = 1").fetchone()[0] == "Retain this"
    assert len(list(tmp_path.glob("legacy-chat.sqlite3.pre-v14-*.bak"))) == 1
    ChatStore(database)
    assert len(list(tmp_path.glob("legacy-chat.sqlite3.pre-v14-*.bak"))) == 1

    restored_path = tmp_path / "restored.sqlite3"
    shutil.copy2(backups[0], restored_path)
    restored = ChatStore(restored_path)
    assert restored.list_messages(conversation.id)[0].content == "Retain this"
    assert restored.get_run(run_id)["request"] == "Feature"


def test_version_three_upgrade_adds_context_links_without_losing_knowledge(tmp_path) -> None:
    database = tmp_path / "version-three.sqlite3"
    store = ChatStore(database)
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    item_id = store.create_knowledge_item(
        project_id=project.id, kind="objective", title="Goal", body="Keep progress",
        rationale="", confidence="unverified", source_ref="user brief",
        source_revision="v1", author="user",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 3")
        connection.execute("DROP TABLE knowledge_links")
        connection.execute("DROP TABLE conversation_context")

    reopened = ChatStore(database)

    assert reopened.get_knowledge_item(item_id).body == "Keep progress"
    assert len(list(tmp_path.glob("version-three.sqlite3.pre-v14-*.bak"))) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == ChatStore.SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


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


def test_running_runs_can_be_recovered_as_needs_attention(tmp_path) -> None:
    store = ChatStore(tmp_path / "recovery.sqlite3")
    project = store.upsert_project("sample", str(tmp_path / "sample"), "python")
    run_id = store.create_run(project.id, "feature", "LOCAL_ONLY")

    recovered = store.recover_interrupted_runs()

    assert recovered == (run_id,)
    assert store.get_run(run_id)["status"] == "needs_attention"


def test_hybrid_preferences_archival_and_run_metadata_are_migrated(tmp_path) -> None:
    store = ChatStore(tmp_path / "hybrid.sqlite3")
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")

    defaults = store.get_project_preferences(project.id)
    assert defaults.provider == "codex"
    assert defaults.strategy == "HYBRID"
    assert defaults.trusted_providers == ()

    store.set_project_preferences(
        project.id,
        provider="claude",
        strategy="FRONTIER_ONLY",
        trusted_providers=("codex", "claude"),
        model_overrides={"claude": "sonnet"},
    )
    configured = store.get_project_preferences(project.id)
    assert configured.provider == "claude"
    assert configured.model_overrides == {"claude": "sonnet"}

    run_id = store.create_run(
        project.id, "feature", "HYBRID", provider="claude", model="sonnet",
        base_commit="abc", worktree="C:/temp/run",
    )
    store.update_run_state(run_id, "ready_to_apply", final_commit="def", recovery_reason="checkout changed")
    run = store.get_run(run_id)
    assert run["provider"] == "claude"
    assert run["final_commit"] == "def"
    assert run["recovery_reason"] == "checkout changed"

    store.archive_project(project.id)
    assert store.list_projects() == []
    assert store.get_run(run_id) is not None
    store.upsert_project("Sample", project.path, "python")
    assert [item.id for item in store.list_projects()] == [project.id]


def test_startup_recovery_marks_all_active_stages_needs_attention(tmp_path) -> None:
    store = ChatStore(tmp_path / "recovery-stages.sqlite3")
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    first = store.create_run(project.id, "one", "HYBRID")
    second = store.create_run(project.id, "two", "HYBRID")
    store.update_run_state(second, "validating")

    recovered = store.recover_interrupted_runs()

    assert recovered == (first, second)
    assert store.get_run(first)["status"] == "needs_attention"
    assert store.get_run(second)["status"] == "needs_attention"


def test_task_acceptance_and_named_checks_persist_through_reopen(tmp_path) -> None:
    path = tmp_path / "task-checks.sqlite3"
    store = ChatStore(path)
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    run_id = store.create_run(project.id, "Parser", "LOCAL_ONLY")
    store.create_run_task(
        run_id, "parser", "Parser", "Implement parser", "low",
        acceptance_criteria=("Accepts valid input", "Rejects invalid input"),
        verification_checks=("pytest", "lint"),
    )

    task = ChatStore(path).list_run_tasks(run_id)[0]
    assert task.acceptance_criteria == ("Accepts valid input", "Rejects invalid input")
    assert task.verification_checks == ("pytest", "lint")


def test_version_ten_task_rows_get_compatible_check_defaults(tmp_path) -> None:
    path = tmp_path / "version-ten.sqlite3"
    store = ChatStore(path)
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    run_id = store.create_run(project.id, "Parser", "LOCAL_ONLY")
    store.create_run_task(run_id, "parser", "Parser", "Implement parser", "low")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 10")
        connection.execute("ALTER TABLE tasks DROP COLUMN acceptance_criteria_json")
        connection.execute("ALTER TABLE tasks DROP COLUMN verification_checks_json")

    reopened = ChatStore(path)
    task = reopened.list_run_tasks(run_id)[0]
    assert task.acceptance_criteria == ("Configured validation must pass",)
    assert task.verification_checks == ("project-default",)
    assert len(list(tmp_path.glob("version-ten.sqlite3.pre-v14-*.bak"))) == 1


def test_completion_record_rejects_model_prose_without_passing_validation(tmp_path) -> None:
    store = ChatStore(tmp_path / "completion.sqlite3")
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    run_id = store.create_run(project.id, "Parser", "LOCAL_ONLY")
    task_id = store.create_run_task(run_id, "parser", "Parser", "Implement parser", "low")
    with pytest.raises(ValueError, match="passing validation"):
        store.record_task_completion(
            run_id, task_id, revision="abc", checks=("pytest",),
            validation_summary="The model says it works", output_artifacts=("src/parser.py",),
            unmet_criteria=(), human_review_status="not_required",
        )
    assert store.get_task_completion(task_id) is None


def test_task_lifecycle_states_and_wait_reasons_survive_restart(tmp_path) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    store = ChatStore(path)
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    run_id = store.create_run(project.id, "Build", "LOCAL_ONLY")
    states = (
        "ready", "running", "verifying", "completed", "blocked", "failed",
        "retrying", "waiting_quota", "waiting_approval", "waiting_auth",
        "cancelled", "review_required",
    )
    for index, state in enumerate(states):
        task_id = store.create_run_task(run_id, f"task-{index}", state, state, "low")
        store.transition_task_state(
            task_id, state, attempts=1, reason=f"fixture reason for {state}", actor="test",
        )
    reopened = ChatStore(path)
    tasks = reopened.list_run_tasks(run_id)
    assert tuple(task.status for task in tasks) == states
    for task in tasks:
        history = reopened.list_task_state_history(task.id)
        assert [item["revision"] for item in history] == [1, 2]
        assert history[1]["reason"] == f"fixture reason for {task.status}"
    with pytest.raises(ValueError, match="unsupported task lifecycle state"):
        reopened.transition_task_state(tasks[0].id, "imaginary", attempts=1, reason="x", actor="test")


def test_restart_moves_active_task_to_review_with_reason(tmp_path) -> None:
    path = tmp_path / "active-task.sqlite3"
    store = ChatStore(path)
    project = store.upsert_project("Sample", str(tmp_path / "sample"), "python")
    run_id = store.create_run(project.id, "Build", "LOCAL_ONLY")
    task_id = store.create_run_task(run_id, "task", "Task", "Do work", "low")
    store.transition_task_state(task_id, "running", attempts=1, reason="started", actor="executor")

    reopened = ChatStore(path)
    assert reopened.recover_interrupted_runs() == (run_id,)
    assert reopened.list_run_tasks(run_id)[0].status == "review_required"
    assert reopened.list_task_state_history(task_id)[-1]["reason"] == "application closed during active task"
    assert reopened.recover_interrupted_runs() == ()
