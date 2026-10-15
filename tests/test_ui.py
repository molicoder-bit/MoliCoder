from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.widgets import ChatBubble
from local_model_chat.molicoder.providers import ProviderStatus
from local_model_chat.kandev.contracts import KandevTaskRef
from PySide6.QtWidgets import QCheckBox, QDialog, QDoubleSpinBox, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTextBrowser
import subprocess
import hashlib
from types import SimpleNamespace


class FakeClient:
    def ensure_running(self):
        return True

    def tags(self):
        return [
            {"name": "molicoder:latest"},
            {"name": "local-reasoning:latest"},
        ]

    def show(self, _model):
        return {"details": {"parameter_size": "7.6B", "quantization_level": "Q4_K_M"}}

    def running_models(self):
        return []

    def unload(self, _model):
        return None


def test_main_window_starts_and_creates_initial_chat(qtbot, tmp_path) -> None:
    window = MainWindow(ChatStore(tmp_path / "ui.sqlite3"), FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: "connected" in window.connection_label.text().lower())
    qtbot.waitUntil(lambda: window.startup_worker is None)
    assert window.current_conversation_id is not None
    assert window.model_combo.count() == 2
    assert window.build_owner.currentData() == "internal"
    assert window.preset.name == "molicoder:latest"
    assert "16,384" in window.model_badge.text()
    assert "reserved output" in window.context_label.text()


def test_streaming_response_scrolls_to_updated_bottom(qtbot, tmp_path) -> None:
    window = MainWindow(ChatStore(tmp_path / "scroll.sqlite3"), FakeClient())
    qtbot.addWidget(window)
    window.resize(1020, 680)
    window.show()
    qtbot.waitUntil(lambda: "connected" in window.connection_label.text().lower())

    window.active_bubble = ChatBubble("assistant")
    window.transcript_layout.insertWidget(
        window.transcript_layout.count() - 1, window.active_bubble
    )
    bar = window.scroll.verticalScrollBar()
    bar.setValue(0)

    window._append_content("\n".join(f"Streaming response line {line}" for line in range(150)))

    qtbot.waitUntil(lambda: bar.maximum() > 0)
    qtbot.wait(50)
    assert bar.value() == bar.maximum()


def test_first_message_in_new_chat_renders_once(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "first-message.sqlite3")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: "connected" in window.connection_label.text().lower())
    monkeypatch.setattr("local_model_chat.ui.main_window.ChatWorker.start", lambda _worker: None)

    window.composer.setPlainText("hi")
    window._send_message()

    user_bubbles = [
        item.widget()
        for index in range(window.transcript_layout.count())
        if (item := window.transcript_layout.itemAt(index))
        and isinstance(item.widget(), ChatBubble)
        and item.widget().role == "user"
    ]
    saved_user_messages = [
        message
        for message in store.list_messages(window.current_conversation_id)
        if message.role == "user"
    ]
    assert len(user_bubbles) == 1
    assert len(saved_user_messages) == 1


def test_current_task_reply_in_chat_uses_sqlite_without_model(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "status-ui.sqlite3")
    project_path = tmp_path / "sample"
    project_path.mkdir()
    project = store.upsert_project("Sample", str(project_path), "python")
    run_id = store.create_run(project.id, "Build feature", "HYBRID")
    task_id = store.create_run_task(run_id, "T1", "Write parser", "Parser", "low")
    store.update_run_task(task_id, "running", 1)
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(
        "local_model_chat.ui.main_window.ChatWorker.start",
        lambda _worker: (_ for _ in ()).throw(AssertionError("status launched model")),
    )

    window.composer.setPlainText("What task are we on?")
    window._send_message()

    messages = store.list_messages(window.current_conversation_id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert f"run #{run_id}" in messages[-1].content
    assert "Write parser" in messages[-1].content
    assert window.chat_worker is None
    assert window.active_bubble is None


def test_implement_request_creates_reviewable_build_proposal(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "proposal.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(
        "local_model_chat.ui.main_window.ChatWorker.start",
        lambda _worker: (_ for _ in ()).throw(AssertionError("proposal launched model")),
    )

    window.composer.setPlainText("Implement a parser")
    window._send_message()

    assert window.pending_build_request == (project.id, "Implement a parser")
    assert not window.pending_build_card.isHidden()
    assert store.list_project_runs(project.id) == []
    window._set_view("build")
    window._set_view("chat")
    assert window.pending_build_request is not None
    window._review_pending_build()
    assert window.view_stack.currentWidget() is window.build_view
    assert window.doctor_button.text() == "Environment doctor"
    assert window.build_request.toPlainText() == "Implement a parser"
    assert window.pending_build_request is None
    assert store.list_project_runs(project.id) == []


def test_pending_build_proposal_can_be_cancelled(qtbot, tmp_path) -> None:
    store = ChatStore(tmp_path / "cancel-proposal.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    window.composer.setPlainText("Build a parser")
    window._send_message()

    window._cancel_pending_build()

    assert window.pending_build_request is None
    assert not window.pending_build_card.isVisible()
    assert store.list_project_runs(project.id) == []


def test_build_request_without_project_waits_for_selection(qtbot, tmp_path) -> None:
    store = ChatStore(tmp_path / "select-project.sqlite3")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window.composer.setPlainText("Implement a parser")

    window._send_message()

    assert window.pending_build_request == (None, "Implement a parser")
    assert not window.pending_build_review.isEnabled()
    window._cancel_pending_build()
    assert window.pending_build_request is None


def test_evaluation_stays_in_read_only_chat_path(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "evaluate.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    launches = []
    monkeypatch.setattr(
        "local_model_chat.ui.main_window.ChatWorker.start",
        lambda _worker: launches.append("chat"),
    )
    window.composer.setPlainText("Evaluate this code")

    window._send_message()

    assert launches == ["chat"]
    assert window.pending_build_request is None
    assert store.list_project_runs(project.id) == []


def test_build_button_records_exact_authorization_before_worker_launch(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "build-approval.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    window.build_strategy.setCurrentIndex(window.build_strategy.findData("LOCAL_OFFLINE"))
    request = "Implement a parser"
    window.build_request.setPlainText(request)
    monkeypatch.setattr(window, "_refresh_build_readiness", lambda: window.build_run_button.setEnabled(True))
    monkeypatch.setattr("local_model_chat.ui.main_window.build_orchestrator", lambda **_kwargs: type("Stub", (), {})())
    observed = []
    monkeypatch.setattr(
        "local_model_chat.ui.main_window.OrchestrationWorker.start",
        lambda _worker: observed.append("started"),
    )

    window._run_build()

    digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
    scope = f"project:{project.id}:provider:codex:mode:LOCAL_OFFLINE:request:{digest}"
    record = store.latest_action_authorization(project_id=project.id, action="build_run", scope=scope)
    assert record is not None
    assert record["risk"] == "mutation"
    assert record["actor"] == "local_user_build_button"
    assert observed == ["started"]


def test_kandev_build_selection_authorizes_exact_remote_task_and_never_uses_internal_owner(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "kandev-build.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    window.build_owner.setCurrentIndex(window.build_owner.findData("kandev"))
    assert not window.build_provider.isEnabled()
    window.build_request.setPlainText("Build parser")
    adapter = SimpleNamespace(
        connection=SimpleNamespace(endpoint="http://127.0.0.1:38429/mcp"),
        api=SimpleNamespace(config=SimpleNamespace(workspace_id="workspace-1", agent_profile_id="agent-1")),
    )
    captured = {}
    monkeypatch.setattr("local_model_chat.ui.main_window.configured_kandev_adapter", lambda _store: adapter)
    monkeypatch.setattr(window, "_refresh_build_readiness", lambda: window.build_run_button.setEnabled(True))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        "local_model_chat.ui.main_window.build_orchestrator",
        lambda **kwargs: (captured.update(kwargs) or SimpleNamespace(on_event=None)),
    )
    monkeypatch.setattr("local_model_chat.ui.main_window.OrchestrationWorker.start", lambda _worker: None)

    window._run_build()

    assert captured["kandev_adapter"] is adapter
    assert captured["kandev_connection"] is adapter.connection
    ref = KandevTaskRef(4, 5, "molicoder:project:1:run:4:task:5")
    captured["kandev_authorize_launch"](ref)
    create = store.latest_action_authorization(
        project_id=None, action="kandev_task_create", scope=f"workspace-1:{ref.external_id}",
    )
    launch = store.latest_action_authorization(
        project_id=None, action="kandev_agent_launch", scope=f"workspace-1:{ref.external_id}:agent-1",
    )
    assert create and launch
    assert create["actor"] == launch["actor"] == "local_user_kandev_build_button"


def test_pending_kandev_run_reconciles_by_owner_without_internal_resume(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "kandev-recovery.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    run_id = store.create_run(project.id, "Build parser", "HYBRID", provider="kandev")
    store.update_run_state(run_id, "needs_attention", recovery_reason="create outcome unknown")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    assert window.build_resume_button.isEnabled()
    assert "Kandev owns run" in window.build_ownership_status.text()
    assert not window.build_discard_button.isEnabled()
    adapter = SimpleNamespace(connection=SimpleNamespace(endpoint="http://127.0.0.1:38429/mcp"))
    captured = {}
    monkeypatch.setattr("local_model_chat.ui.main_window.configured_kandev_adapter", lambda _store: adapter)
    monkeypatch.setattr(
        "local_model_chat.ui.main_window.build_orchestrator",
        lambda **kwargs: (captured.update(kwargs) or SimpleNamespace(on_event=None)),
    )
    monkeypatch.setattr("local_model_chat.ui.main_window.OrchestrationWorker.start", lambda _worker: None)

    window._resume_preserved_run()

    assert captured["kandev_adapter"] is adapter
    assert window.orchestration_worker.resume_run_id == run_id
    assert window.orchestration_worker.orchestrator.on_event == window.orchestration_worker.relay_event


def test_accepted_kandev_run_restores_apply_action_after_restart(qtbot, tmp_path) -> None:
    store = ChatStore(tmp_path / "kandev-apply.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    worktree = tmp_path / "kandev-worktree"
    worktree.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    run_id = store.create_run(project.id, "Build parser", "HYBRID", provider="kandev")
    task_id = store.create_run_task(run_id, "K1", "Kandev Build task", "Build parser", "high")
    store.update_run_task(task_id, "completed", 0)
    store.update_task_workspace(task_id, worktree=str(worktree), branch="feature/task", base_commit="base")
    store.update_run_state(run_id, "ready_to_apply", worktree=str(worktree), base_commit="base", final_commit="head")

    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)

    assert window.last_run_id == run_id
    assert window.build_merge_button.isEnabled()
    assert window.build_merge_button.text() == "Apply accepted Kandev result"


def test_kandev_apply_button_uses_fenced_backend_operation(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "kandev-apply-route.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    worktree = tmp_path / "kandev-worktree"
    worktree.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    run_id = store.create_run(project.id, "Build parser", "HYBRID", provider="kandev")
    task_id = store.create_run_task(run_id, "K1", "Kandev Build task", "Build parser", "high")
    store.update_run_task(task_id, "completed", 0)
    store.update_task_workspace(task_id, worktree=str(worktree), branch="feature/task", base_commit="base")
    store.update_run_state(run_id, "ready_to_apply", worktree=str(worktree), base_commit="base", final_commit="head")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    adapter = SimpleNamespace(connection=SimpleNamespace(endpoint="http://127.0.0.1:38429/mcp"))
    applied = []

    class FakeOrchestrator:
        def apply(self, selected_run_id):
            applied.append(selected_run_id)
            store.finish_run(selected_run_id, "completed")
            return SimpleNamespace(
                run_id=selected_run_id, status="completed", completed_tasks=1, total_tasks=1,
                integration_branch="feature/task", integration_path=worktree, base_commit="base", error="",
            )

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr("local_model_chat.ui.main_window.configured_kandev_adapter", lambda _store: adapter)
    monkeypatch.setattr("local_model_chat.ui.main_window.build_orchestrator", lambda **_kwargs: FakeOrchestrator())

    window._merge_last_build()

    assert applied == [run_id]
    assert store.get_run(run_id)["status"] == "completed"


def test_build_button_authorizes_only_explicit_selected_fallback(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "fallback-approval.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    store.set_project_preferences(project.id, provider="codex", strategy="HYBRID",
                                  trusted_providers=("codex", "copilot"), model_overrides={})
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    window.build_strategy.setCurrentIndex(window.build_strategy.findData("HYBRID"))
    window.build_fallback_provider.setCurrentIndex(window.build_fallback_provider.findData("copilot"))
    request = "Implement a parser"
    window.build_request.setPlainText(request)
    monkeypatch.setattr(window, "_refresh_build_readiness", lambda: window.build_run_button.setEnabled(True))
    captured = {}
    monkeypatch.setattr(
        "local_model_chat.ui.main_window.build_orchestrator",
        lambda **kwargs: (captured.update(kwargs) or type("Stub", (), {})()),
    )
    monkeypatch.setattr("local_model_chat.ui.main_window.OrchestrationWorker.start", lambda _worker: None)
    window._run_build()
    digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
    scope = f"project:{project.id}:provider:copilot:mode:HYBRID:request:{digest}:fallback"
    record = store.latest_action_authorization(project_id=project.id, action="build_run", scope=scope)
    assert record is not None and record["risk"] == "external"
    assert captured["fallback_provider_name"] == "copilot"
    assert captured["fallback_authorization_scope"] == scope


def test_conversation_switch_restores_linked_project(qtbot, tmp_path) -> None:
    db = tmp_path / "conversation-project.sqlite3"
    store = ChatStore(db)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = store.upsert_project("First", str(first_root), "python")
    second = store.upsert_project("Second", str(second_root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=first.id)
    window._new_chat()
    first_chat = window.current_conversation_id
    window._load_project_list(select_id=second.id)
    window._new_chat()
    second_chat = window.current_conversation_id

    window._load_conversation_list(select_id=first_chat)

    assert first_chat != second_chat
    assert window.current_project_id == first.id
    assert ChatStore(db).get_conversation_project_id(first_chat) == first.id
    assert ChatStore(db).get_conversation_project_id(second_chat) == second.id


def test_project_shell_adds_project_and_switches_views(qtbot, tmp_path) -> None:
    project = tmp_path / "sample-project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    window = MainWindow(ChatStore(tmp_path / "projects.sqlite3"), FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: "connected" in window.connection_label.text().lower())

    added = window._add_project_path(project)
    window._set_view("build")

    assert added.kind == "python"
    assert "No monthly paid API limit" in window.pacing_label.text()
    assert window.project_list.count() == 1
    assert window.view_stack.currentWidget() is window.build_view
    assert "sample-project" in window.build_project_title.text()
    assert window.current_conversation_id is not None
    assert (project / ".molicoder" / "project.json").is_file()

    window._set_view("chat")
    assert window.view_stack.currentWidget() is window.chat_view


def test_new_project_is_immediately_ready_for_a_build(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "local_model_chat.molicoder.projectstate._setup_python_environment",
        lambda _root: None,
    )
    window = MainWindow(ChatStore(tmp_path / "new-project.sqlite3"), FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)

    project = window._create_project_at(tmp_path, "ready-project", "python")
    window._set_view("build")
    window.build_request.setPlainText("Create a small expense tracker with tests")

    assert project.kind == "python"
    assert window.registry.selected().id == project.id
    assert window.build_run_button.isEnabled()
    assert "Ready" in window.build_status.text()
    assert (tmp_path / "ready-project" / "src" / "ready_project" / "__init__.py").is_file()
    assert (tmp_path / "ready-project" / "tests" / "test_smoke.py").is_file()
    assert window.build_provider.currentData() == "codex"
    assert window.build_strategy.currentData() == "HYBRID"


def test_build_run_requires_clean_named_branch_and_request(qtbot, tmp_path) -> None:
    project = tmp_path / "safe-project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='safe'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    window = MainWindow(ChatStore(tmp_path / "build.sqlite3"), FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: "connected" in window.connection_label.text().lower())
    window._add_project_path(project)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)

    window._update_build_project(window.registry.selected())
    window._set_view("build")
    assert not window.build_run_button.isEnabled()

    window.build_request.setPlainText("Add a focused unit test")
    assert window.build_run_button.isEnabled()
    assert "Ready" in window.build_status.text()

    (project / "dirty.py").write_text("x = 1\n", encoding="utf-8")
    window._update_build_project(window.registry.selected())
    assert not window.build_run_button.isEnabled()
    assert "clean" in window.build_status.text().lower()


def test_build_failure_shows_diagnostics_and_run_log(qtbot, tmp_path) -> None:
    window = MainWindow(ChatStore(tmp_path / "diagnostics.sqlite3"), FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    run_log = tmp_path / "project" / ".molicoder" / "logs" / "run-7.jsonl"

    window._build_event(
        "run_started",
        {"request": "build a calculator", "mode": "LOCAL_ONLY", "log_path": str(run_log)},
    )
    window._build_event(
        "task_failed",
        {
            "key": "task-1",
            "title": "Create project structure",
            "reason": "attempt_budget",
            "executor_error": "turn budget exceeded",
            "turns": 4,
            "tool_calls": 0,
            "prompt_tokens": 7053,
            "output_tokens": 240,
            "trace": ["turn 4: no tool call; model said: I should inspect first"],
            "worktree": str(tmp_path / "worktree"),
        },
    )

    assert window.last_run_log_path == run_log
    assert window.build_log_button.isEnabled()
    details = window.build_details.toPlainText().lower()
    assert "turn budget exceeded" in details
    assert "no tool call" in details
    assert "7,053" in details
    assert "run log" in details


def test_build_route_decision_is_readable_in_details(qtbot, tmp_path) -> None:
    window = MainWindow(ChatStore(tmp_path / "route.sqlite3"), FakeClient())
    qtbot.addWidget(window)
    window._build_event("routing_decision", {
        "selected": "opaque-cli:codex", "reason": "explicit Build selection",
        "constraints": ["internal usage unknown"], "alternatives": ["ollama-local:molicoder:latest"],
        "usage_accuracy": "unknown",
    })
    window.build_events.setCurrentRow(window.build_events.count() - 1)
    details = window.build_details.toPlainText()
    assert "opaque-cli:codex" in details
    assert "internal usage unknown" in details
    assert "ollama-local:molicoder:latest" in details
    window._build_event("routing_usage", {
        "local_requests": 1, "local_input_tokens": 10, "local_output_tokens": 3,
        "opaque_cli_calls": 1, "opaque_cli_reported_input_tokens": 5,
        "opaque_cli_reported_output_tokens": 2,
    })
    window.build_events.setCurrentRow(window.build_events.count() - 1)
    assert "cost unknown" in window.build_details.toPlainText()


def test_provider_settings_distinguishes_verified_auth_from_installed_cli(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "provider-settings.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(
        "local_model_chat.ui.main_window.probe_all_providers",
        lambda: (
            ProviderStatus("codex", True, True, version="codex 1", auth_verified=False),
            ProviderStatus("claude", True, True, version="claude 1", auth_verified=True),
            ProviderStatus("copilot", False, False, detail="not installed"),
        ),
    )
    captured = {}

    def inspect_dialog(dialog):
        captured["labels"] = "\n".join(label.text() for label in dialog.findChildren(QLabel))
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", inspect_dialog)
    window._provider_settings()

    labels = captured["labels"]
    assert "Codex — Opaque subscription CLI" in labels
    assert "Installed; sign-in and entitlement unverified" in labels
    assert "Sign-in verified" in labels
    assert "Copilot — Opaque subscription CLI" in labels
    assert "OpenCode — Local gateway preview" in labels
    assert "Build selection unavailable" in labels
    assert "Sandbox: CLI tool permissions" in labels
    assert "Capabilities: planning, editing" in labels
    assert "Unavailable: CLI is not installed" in labels
    assert "Opaque subscription CLI" in window.build_agent_info.text()
    assert "external side effects" in window.build_agent_info.text()
    window.build_provider.setCurrentIndex(window.build_provider.findData("claude"))
    assert "operating-system sandbox" in window.build_agent_info.text()


def test_provider_settings_shows_registry_and_saves_offering_enable_control(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "offerings.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    registry = window.inference_registry
    registry.add_provider("fixture-provider", "Fixture Provider", "https://example.test/provider")
    registry.add_model("fixture-model", "fixture", "1")
    registry.add_offering("fixture-offering", "fixture-provider", "fixture-model", "direct", "recurring_free")
    captured = {}

    def inspect_and_enable(dialog):
        labels = "\n".join(label.text() for label in dialog.findChildren(QLabel))
        captured["labels"] = labels
        control = next(box for box in dialog.findChildren(QCheckBox) if box.text() == "Enable fixture-offering")
        control.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", inspect_and_enable)
    window._provider_settings()
    assert "fixture-provider / fixture-model (fixture-offering) — recurring_free" in captured["labels"]
    assert "Auth: unknown; health: unknown; status: discovered" in captured["labels"]
    assert "Capabilities: unknown; privacy:" in captured["labels"]
    assert "quota: unknown" in captured["labels"]
    assert "Price checked: unknown; terms checked: unknown" in captured["labels"]
    assert registry.get_offering("fixture-offering").status == "eligible"


def test_custom_provider_can_be_registered_from_ui_without_activation(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "custom-provider.sqlite3")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    def fill_dialog(dialog):
        edits = {item.placeholderText(): item for item in dialog.findChildren(QLineEdit)}
        edits["my-provider"].setText("custom-api")
        edits["Provider display name"].setText("Custom API")
        edits["https://provider.example/v1"].setText("https://api.example.test/v1")
        edits["provider-model-id"].setText("sample-model")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", fill_dialog)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    window._add_custom_provider()
    offerings = [item for item in window.inference_registry.list_offerings()
                 if item.provider_id == "custom-api"]
    assert len(offerings) == 1 and offerings[0].status == "discovered"


def test_official_api_preset_registration_remains_inactive(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "preset-provider.sqlite3")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *_args: ("OpenAI API", True))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args: ("example-model", True))
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    window._add_api_preset()
    offerings = [item for item in window.inference_registry.list_offerings()
                 if item.provider_id == "openai-api"]
    assert len(offerings) == 1
    assert offerings[0].status == "discovered"
    assert offerings[0].input_usd_per_million is None


def test_custom_provider_connection_check_is_available_in_settings(qtbot, tmp_path, monkeypatch) -> None:
    from local_model_chat.inference.onboarding import ConnectionCheck, CustomRoute, register_custom_route

    store = ChatStore(tmp_path / "custom-check.sqlite3")
    project_root = tmp_path / "sample"
    project_root.mkdir()
    project = store.upsert_project("Sample", str(project_root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    offering_id = register_custom_route(store, CustomRoute(
        "local-api", "Local API", "http://127.0.0.1:12345/v1", "model-a",
    ))
    captured = {}

    def inspect_dialog(dialog):
        buttons = [button.text() for button in dialog.findChildren(QPushButton)]
        assert "Check custom provider connection" in buttons
        assert "Configure custom provider facts" in buttons
        assert "Run capped custom API test" in buttons
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", inspect_dialog)
    window._provider_settings()
    monkeypatch.setattr(QInputDialog, "getItem", lambda *_args: ("local-api / model-a", True))
    monkeypatch.setattr(
        "local_model_chat.ui.main_window.check_custom_connection",
        lambda _store, selected_id, **_kwargs: (
            captured.update(offering_id=selected_id) or ConnectionCheck("ready", "Connected.")
        ),
    )
    monkeypatch.setattr(QMessageBox, "information", lambda _parent, _title, detail: captured.update(detail=detail))
    window._check_custom_provider_connection(tuple(window.inference_registry.list_offerings()))
    assert captured["offering_id"] == offering_id
    assert "stays inactive" in captured["detail"]


def test_custom_api_test_waits_for_exact_user_confirmation(qtbot, tmp_path, monkeypatch) -> None:
    from local_model_chat.inference.onboarding import (
        CustomRoute, CustomRouteFacts, attest_custom_route_facts, register_custom_route,
    )
    from local_model_chat.workers import CustomApiWorker

    store = ChatStore(tmp_path / "custom-test.sqlite3")
    root = tmp_path / "project"
    root.mkdir()
    project = store.upsert_project("Project", str(root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    offering_id = register_custom_route(store, CustomRoute(
        "local-api", "Local API", "http://127.0.0.1:12345/v1", "model-a",
    ))
    attest_custom_route_facts(store, CustomRouteFacts(
        offering_id, "https://example.test/model", "", "private", 8192, 512,
        None, None, True,
    ))
    window.inference_registry.set_offering_state(
        offering_id, status="eligible", health_state="healthy", actor="user", reason="fixture",
    )
    offering = window.inference_registry.get_offering(offering_id)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *_args: ("local-api / model-a", True))
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *_args: ("Hi", True))
    captured = {}
    monkeypatch.setattr(CustomApiWorker, "start", lambda worker: captured.update(worker=worker))
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda _parent, _title, detail: (
            captured.update(detail=detail) or QMessageBox.StandardButton.No
        ),
    )
    window._run_custom_api_test((offering,))
    assert "worker" not in captured
    assert offering.route_id in captured["detail"]
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window._run_custom_api_test((offering,))
    assert captured["worker"].offering_id == offering_id


def test_doctor_refresh_updates_states_and_keeps_chat_available(qtbot, tmp_path, monkeypatch) -> None:
    from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
    from local_model_chat.environment.doctor import doctor_report
    from local_model_chat.environment.manifests import load_manifests
    from local_model_chat.workers import DoctorWorker

    environment = EnvironmentReport(
        1, "Windows", "x86_64", 4, 8 * 1024**3, 10 * 1024**3, "unknown", "fixture",
        (DiscoveredTool("python", "C:/Python/python.exe", "3.13.0", "installed"),
         DiscoveredTool("codex", "C:/Tools/codex.exe", "1.0.0", "installed")),
        (),
    )
    scans = iter(("login_needed", "permission_needed"))

    def fake_run(worker):
        worker.ready.emit(doctor_report(
            environment, load_manifests(), auth_states={"codex": next(scans)},
        ))

    monkeypatch.setattr(DoctorWorker, "run", fake_run)
    window = MainWindow(ChatStore(tmp_path / "doctor.sqlite3"), FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._show_environment_doctor()
    dialog = window.doctor_dialog
    assert dialog is not None
    status = dialog.findChildren(QLabel)[0]
    refresh = next(button for button in dialog.findChildren(QPushButton) if button.text() == "Refresh checks")
    report = dialog.findChild(QTextBrowser)
    qtbot.waitUntil(lambda: "Waiting login: 1" in status.text())
    assert "Sign in" in report.toPlainText()
    assert window.model_combo.isEnabled()
    qtbot.waitUntil(refresh.isEnabled)
    refresh.click()
    qtbot.waitUntil(lambda: "Waiting permission: 1" in status.text())
    assert "Grant the needed permission" in report.toPlainText()
    dialog.close()


def test_managed_install_button_records_exact_in_app_approval(qtbot, tmp_path, monkeypatch) -> None:
    from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
    from local_model_chat.environment.doctor import doctor_report
    from local_model_chat.environment.manifests import load_manifests
    from local_model_chat.workers import DoctorWorker, ManagedInstallWorker

    environment = EnvironmentReport(
        1, "Windows", "x86_64", 4, 8 * 1024**3, 10 * 1024**3, "unknown", "fixture",
        (DiscoveredTool("kandev", None, None, "missing"),), (),
    )
    monkeypatch.setattr(
        DoctorWorker, "run",
        lambda worker: worker.ready.emit(doctor_report(environment, load_manifests())),
    )
    captured = {}
    monkeypatch.setattr(QInputDialog, "getItem", lambda *_args: ("kandev 0.97.0", True))
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda _dialog, _title, detail: (
            captured.update(detail=detail) or QMessageBox.StandardButton.Yes
        ),
    )
    monkeypatch.setattr(
        ManagedInstallWorker, "start",
        lambda worker: captured.update(plan=worker.plan, manifest=worker.manifest),
    )
    store = ChatStore(tmp_path / "setup-ui.sqlite3")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._show_environment_doctor()
    dialog = window.doctor_dialog
    button = next(item for item in dialog.findChildren(QPushButton)
                  if item.text() == "Install supported managed tool")
    qtbot.waitUntil(button.isEnabled)
    button.click()
    assert captured["plan"].authorized
    assert captured["manifest"].tool_id == "kandev"
    assert captured["manifest"].sha256 in captured["detail"]
    approval = store.latest_action_authorization(
        project_id=None, action="setup", scope=captured["plan"].scope,
    )
    assert approval["decision"] == "approved" and approval["actor"] == "user"
    dialog.close()


def test_usage_dialog_shows_source_aware_project_report(qtbot, tmp_path, monkeypatch) -> None:
    from local_model_chat.inference.budget import BudgetManager
    store = ChatStore(tmp_path / "usage-ui.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    window._show_usage_budgets()
    dialog = next(item for item in window.findChildren(QDialog) if item.windowTitle() == "Usage and budgets")
    report = dialog.findChild(QTextBrowser).toPlainText()
    assert "Project hard API limit: unconfigured" in report
    assert "Opaque subscription CLI calls: 0; internal usage and cost unknown" in report
    assert "Latest Build route: no recorded decision" in report
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    dialog.findChild(QDoubleSpinBox).setValue(2.5)
    next(button for button in dialog.findChildren(QPushButton)
         if button.text() == "Save hard limit").click()
    assert BudgetManager(store).project_limit(project.id) == 2.5
    assert "Project hard API limit: $2.5000" in dialog.findChild(QTextBrowser).toPlainText()
    dialog.close()


def test_task_evidence_dialog_exports_safe_trace(qtbot, tmp_path, monkeypatch) -> None:
    store = ChatStore(tmp_path / "evidence-ui.sqlite3")
    root = tmp_path / "sample"
    root.mkdir()
    project = store.upsert_project("Sample", str(root), "python")
    run_id = store.create_run(project.id, "private prompt", "HYBRID")
    store.add_event(run_id, "run_stage", {"stage": "planning", "request": "private prompt"})
    window = MainWindow(store, FakeClient())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.startup_worker is None)
    window._load_project_list(select_id=project.id)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: QMessageBox.StandardButton.Ok)
    window._show_task_evidence()
    dialog = next(item for item in window.findChildren(QDialog) if item.windowTitle() == "Task evidence")
    export = next(button for button in dialog.findChildren(QPushButton)
                  if button.text() == "Export privacy-safe trace")
    export.click()
    outputs = list((root / ".molicoder" / "trace-exports").glob("trace-*.jsonl"))
    assert len(outputs) == 1
    assert "private prompt" not in outputs[0].read_text(encoding="utf-8")
    dialog.close()
