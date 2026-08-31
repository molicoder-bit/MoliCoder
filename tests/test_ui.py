from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.widgets import ChatBubble
import subprocess


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
    assert window.project_list.count() == 1
    assert window.view_stack.currentWidget() is window.build_view
    assert "sample-project" in window.build_project_title.text()
    assert window.current_conversation_id is not None
    assert (project / ".molicoder" / "project.json").is_file()

    window._set_view("chat")
    assert window.view_stack.currentWidget() is window.chat_view


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
