from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow


class FakeClient:
    def ensure_running(self):
        return True

    def tags(self):
        return [
            {"name": "local-coding:latest"},
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
    assert window.current_conversation_id is not None
    assert window.model_combo.count() == 2
    assert window.preset.name == "local-coding:latest"
    assert "8,192" in window.model_badge.text()
    assert "reserved output" in window.context_label.text()
