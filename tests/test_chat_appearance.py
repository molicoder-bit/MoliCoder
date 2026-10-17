import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QContextMenuEvent, QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMenu, QMessageBox

from local_model_chat.domain import GenerationMetrics, Message
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.settings_window import show_settings_window
from local_model_chat.ui.theme import apply_theme
from local_model_chat.ui.workspace import refresh_workspace


@pytest.fixture(autouse=True)
def render_with_real_fonts(qapp):
    # Windows' offscreen plugin does not discover system fonts automatically.
    for filename in ('segoeui.ttf', 'segoeuib.ttf', 'arial.ttf', 'consola.ttf'):
        font = Path('C:/Windows/Fonts') / filename
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))


def make_window(qtbot, tmp_path):
    w = MainWindow(ChatStore(tmp_path / 'appearance.db'), start_ollama_check=False)
    qtbot.addWidget(w)
    w.resize(1200, 900)
    w.show()
    return w


def add_reply(w, text='The README describes the project.', receipt=None):
    mid = w.store.add_message(Message(None, w.current_conversation_id, 'assistant', text))
    if receipt is not None:
        w.store.set_message_receipt(mid, receipt)
    w._render_messages(w._current_messages())
    return w.transcript_selection._bubbles[-1]


@pytest.mark.parametrize('theme', ['light', 'matrix'])
def test_actual_right_click_menu_follows_theme_and_copies(qtbot, tmp_path, theme):
    w = make_window(qtbot, tmp_path)
    bubble = add_reply(w)
    apply_theme(w, theme)
    qtbot.wait(30)
    w.transcript_selection.select_all()
    observed = []

    def inspect_menu():
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        try:
            pixels = menu.grab().toImage()
            observed.append(pixels.pixelColor(8, 8).lightness())
            copy = next(a for a in menu.actions() if a.text() == 'Copy selection')
            assert copy.isEnabled()
            copy.trigger()
        finally:
            menu.close()

    QTimer.singleShot(0, inspect_menu)
    point = bubble.body.viewport().rect().center()
    event = QContextMenuEvent(QContextMenuEvent.Mouse, point, bubble.body.viewport().mapToGlobal(point))
    QApplication.sendEvent(bubble.body.viewport(), event)
    assert len(observed) == 1
    assert observed[0] > 220 if theme == 'light' else observed[0] < 30
    assert QApplication.clipboard().text() == bubble.content


def test_font_size_and_density_apply_live_preserve_selection_and_restore(qtbot, tmp_path):
    w = make_window(qtbot, tmp_path)
    bubble = add_reply(w, receipt={'provider': 'ollama', 'requested_model': 'local', 'response_seconds': 12.3})
    w.composer.setPlainText('Keep my unsent draft')
    w.transcript_selection.select_all()
    selected = w.transcript_selection.selected_text()
    show_settings_window(w)
    family = next(f for f in ('Arial', 'Consolas', 'DejaVu Sans') if f in QFontDatabase.families())
    w._settings_font_control.setCurrentFont(QFont(family))
    w._settings_text_size_control.setValue(21)
    qtbot.waitUntil(lambda: bubble.body.font().pixelSize() == 21)
    assert bubble.body.font().family() == family
    assert bubble.body.document().defaultFont().family() == family
    assert w.composer.font().family() == family and w.project_switch.font().family() == family
    assert w.transcript_selection.selected_text() == selected
    assert w.composer.toPlainText() == 'Keep my unsent draft'
    compact = bubble.sizeHint().height()
    w._settings_density_control.setCurrentIndex(1)
    assert bubble.sizeHint().height() > compact
    w._settings_dialog.close()
    w.close()
    reopened = MainWindow(ChatStore(tmp_path / 'appearance.db'), start_ollama_check=False)
    qtbot.addWidget(reopened)
    show_settings_window(reopened)
    assert reopened.composer.font().family() == family
    assert reopened.composer.font().pixelSize() == 21
    assert reopened._settings_density_control.currentData() == 'comfortable'
    reopened._settings_dialog.close()


def test_compact_cards_and_composer_fit_content_and_grow_for_drafts(qtbot, tmp_path):
    w = make_window(qtbot, tmp_path)
    for i in range(6):
        w.store.add_message(Message(None, w.current_conversation_id, 'user' if i % 2 == 0 else 'assistant', 'One short sentence.'))
    w._render_messages(w._current_messages())
    qtbot.wait(50)
    assert all(b.height() < 110 for b in w.transcript_selection._bubbles)
    assert w.composer.height() < 80
    empty = w.composer.height()
    w.composer.setPlainText('\n'.join(f'Draft line {i}' for i in range(15)))
    qtbot.waitUntil(lambda: w.composer.height() > empty)
    assert w.composer.height() <= 160
    w.composer.clear()
    qtbot.waitUntil(lambda: w.composer.height() == empty)


def test_reply_time_includes_entire_request_and_survives_reloading(qtbot, tmp_path):
    w = make_window(qtbot, tmp_path)
    bubble = add_reply(w)
    w.active_bubble = bubble
    w.chat_worker = SimpleNamespace(deleteLater=lambda: None)
    w._pending_receipt = {'provider': 'opencode', 'requested_model': 'free', 'attempts': [
        {'provider': 'ollama', 'status': 'failed'}, {'provider': 'opencode', 'status': 'completed'}]}
    w._response_started_at = time.monotonic() - 65.2
    w._generation_finished(GenerationMetrics(total_seconds=2.0))
    reply = w._current_messages()[-1]
    receipt = w.store.message_receipt(reply.id)
    assert 65 <= receipt['response_seconds'] < 70
    rendered = w.transcript_selection._bubbles[-1]
    assert rendered.elapsed_label.text() == '1m 5s'
    w._load_conversation_list(select_id=w.current_conversation_id)
    assert w.transcript_selection._bubbles[-1].elapsed_label.text() == '1m 5s'
    assert receipt['metrics']['total_seconds'] == 2.0


def test_local_reply_is_timed_and_older_missing_measurements_stay_unknown(qtbot, tmp_path):
    w = make_window(qtbot, tmp_path)
    w.composer.setPlainText('What project are we on?')
    w.send_button.click()
    receipt = w.store.message_receipt(w._current_messages()[-1].id)
    assert receipt['model_source'] == 'no_inference'
    assert receipt['response_seconds'] >= 0
    assert not w.transcript_selection._bubbles[-1].elapsed_label.isHidden()
    old = add_reply(w, 'An older reply without timing')
    assert old.elapsed_label.isHidden()
    unmeasured = add_reply(w, receipt={'metrics': {'total_seconds': 0}})
    assert unmeasured.elapsed_label.isHidden()
    measured = add_reply(w, receipt={'metrics': {'total_seconds': 7.8}})
    assert measured.elapsed_label.text() == '7.8s'


def test_failed_reply_keeps_its_time_and_error(qtbot, tmp_path, monkeypatch):
    w = make_window(qtbot, tmp_path)
    w.active_bubble = add_reply(w)
    w._response_started_at = time.monotonic() - 3
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: None)
    w._generation_failed('Connection interrupted')
    message = w._current_messages()[-1]
    assert 'Connection interrupted' in message.content
    assert w.store.message_receipt(message.id)['response_seconds'] >= 3
    assert not w.transcript_selection._bubbles[-1].elapsed_label.isHidden()


def test_main_page_folder_and_project_buttons_use_real_registration(qtbot, tmp_path, monkeypatch):
    w = make_window(qtbot, tmp_path)
    folder = tmp_path / 'existing folder'; folder.mkdir()
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(folder))
    assert w.open_folder_button.isVisible() and w.new_project_button.isVisible()
    w.open_folder_button.click()
    assert w.registry.selected().path == str(folder.resolve())
    assert (folder / '.molicoder' / 'project.json').is_file()
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(tmp_path))
    monkeypatch.setattr(QInputDialog, 'getText', lambda *args: ('fresh-node', True))
    monkeypatch.setattr(QInputDialog, 'getItem', lambda *args: ('Node.js', True))
    w.new_project_button.click()
    assert w.registry.selected().path == str((tmp_path / 'fresh-node').resolve())
    assert (tmp_path / 'fresh-node' / 'package.json').is_file()
    assert (tmp_path / 'fresh-node' / '.git').is_dir()
    w.orchestration_worker = SimpleNamespace(paused=False, isRunning=lambda: False)
    w._refresh_build_readiness()
    assert not w.open_folder_button.isEnabled() and not w.new_project_button.isEnabled()
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: pytest.fail('Build must keep its project target'))
    w._add_project()
    w._create_project()
    w.orchestration_worker = None
