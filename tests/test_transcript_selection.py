import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QTextCursor
from PySide6.QtWidgets import QApplication

from local_model_chat.domain import Message
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.ui.theme import apply_theme
from test_ui import FakeClient


def window(qtbot, tmp_path):
    w = MainWindow(ChatStore(tmp_path / 'selection.db'), FakeClient(), start_ollama_check=False)
    qtbot.addWidget(w)
    for role, content in [('user', 'Alpha user question'), ('assistant', 'Beta assistant answer')]:
        w.store.add_message(Message(None, w.current_conversation_id, role, content))
    w._load_conversation_list(select_id=w.current_conversation_id)
    w.resize(1020, 900)
    w.show()
    qtbot.wait(30)
    return w


def point(view, position):
    cursor = QTextCursor(view.document())
    cursor.setPosition(position)
    return view.viewport().mapToGlobal(view.cursorRect(cursor).center())


def mouse(viewport, kind, global_point, *, modifiers=Qt.NoModifier):
    button = Qt.NoButton if kind == QEvent.MouseMove else Qt.LeftButton
    buttons = Qt.NoButton if kind == QEvent.MouseButtonRelease else Qt.LeftButton
    event = QMouseEvent(kind, QPointF(viewport.mapFromGlobal(global_point)), QPointF(global_point), button, buttons, modifiers)
    QApplication.sendEvent(viewport, event)


@pytest.mark.parametrize('reverse', [False, True])
def test_drag_selects_and_copies_across_cards(qtbot, tmp_path, reverse):
    w = window(qtbot, tmp_path)
    first, last = [b.body for b in w.transcript_selection._bubbles]
    source, start, end = (last, point(last, 4), point(first, 0)) if reverse else (first, point(first, 0), point(last, 4))
    mouse(source.viewport(), QEvent.MouseButtonPress, start)
    mouse(source.viewport(), QEvent.MouseMove, end)
    mouse(source.viewport(), QEvent.MouseButtonRelease, end)
    expected = 'Alpha user question\n\nBeta'
    assert w.transcript_selection.selected_text() == expected
    qtbot.keyClick(source, Qt.Key_C, Qt.ControlModifier)
    assert QApplication.clipboard().text() == expected


def test_shift_click_all_and_composer_shortcuts_stay_separate(qtbot, tmp_path):
    w = window(qtbot, tmp_path)
    first, last = [b.body for b in w.transcript_selection._bubbles]
    mouse(first.viewport(), QEvent.MouseButtonPress, point(first, 6))
    mouse(first.viewport(), QEvent.MouseButtonRelease, point(first, 6))
    mouse(last.viewport(), QEvent.MouseButtonPress, point(last, 4), modifiers=Qt.ShiftModifier)
    mouse(last.viewport(), QEvent.MouseButtonRelease, point(last, 4), modifiers=Qt.ShiftModifier)
    assert w.transcript_selection.selected_text() == 'user question\n\nBeta'
    qtbot.keyClick(first, Qt.Key_A, Qt.ControlModifier)
    qtbot.keyClick(first, Qt.Key_C, Qt.ControlModifier)
    assert QApplication.clipboard().text() == 'Alpha user question\n\nBeta assistant answer'
    w.composer.setPlainText('Unsent private draft')
    qtbot.keyClick(w.composer, Qt.Key_A, Qt.ControlModifier)
    qtbot.keyClick(w.composer, Qt.Key_C, Qt.ControlModifier)
    assert QApplication.clipboard().text() == 'Unsent private draft'
    w.copy_chat_button.click()
    assert QApplication.clipboard().text() == 'You:\nAlpha user question\n\nMoliCoder:\nBeta assistant answer'


@pytest.mark.parametrize('theme', ['light', 'matrix'])
def test_selection_survives_stream_theme_and_same_chat_refresh(qtbot, tmp_path, theme):
    w = window(qtbot, tmp_path)
    w.select_chat_button.click()
    expected = w.transcript_selection.selected_text()
    bubble = w.transcript_selection._bubbles[-1]
    bubble.append_content(' Additional streamed words.')
    assert w.transcript_selection.selected_text() == expected
    apply_theme(w, theme, persist=False)
    assert w.transcript_selection.selected_text() == expected
    w._load_conversation_list(select_id=w.current_conversation_id)
    qtbot.waitUntil(lambda: w.transcript_selection.selected_text() == expected)
    w.new_chat_button.click()
    assert not w.transcript_selection.has_selection()
    w.copy_chat_button.click()
    assert QApplication.clipboard().text() == ''


def test_cross_card_drag_auto_scrolls_without_jumping_to_bottom(qtbot, tmp_path):
    w = window(qtbot, tmp_path)
    for index in range(20):
        w.store.add_message(Message(None, w.current_conversation_id, 'assistant', f'Long conversation reply {index}'))
    w._load_conversation_list(select_id=w.current_conversation_id)
    qtbot.wait(30)
    bar = w.scroll.verticalScrollBar()
    bar.setValue(0)
    first = w.transcript_selection._bubbles[0].body
    mouse(first.viewport(), QEvent.MouseButtonPress, point(first, 0))
    edge = w.scroll.viewport().mapToGlobal(w.scroll.viewport().rect().bottomLeft())
    mouse(first.viewport(), QEvent.MouseMove, edge)
    qtbot.waitUntil(lambda: bar.value() > 0)
    mouse(first.viewport(), QEvent.MouseButtonRelease, edge)
    assert 0 < bar.value() < bar.maximum()
    assert 'Alpha user question' in w.transcript_selection.selected_text()
    assert not w.transcript_selection._timer.isActive()
