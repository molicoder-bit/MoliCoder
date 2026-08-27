from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy

from local_model_chat.ui.widgets import MessageComposer


def test_enter_requests_send(qtbot) -> None:
    composer = MessageComposer()
    qtbot.addWidget(composer)
    composer.setPlainText("hello")
    spy = QSignalSpy(composer.send_requested)

    qtbot.keyClick(composer, Qt.Key.Key_Return)

    assert spy.count() == 1
    assert composer.toPlainText() == "hello"


def test_shift_enter_inserts_line_break_without_sending(qtbot) -> None:
    composer = MessageComposer()
    qtbot.addWidget(composer)
    composer.setPlainText("hello")
    composer.moveCursor(composer.textCursor().MoveOperation.End)
    spy = QSignalSpy(composer.send_requested)

    qtbot.keyClick(composer, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)

    assert spy.count() == 0
    assert composer.toPlainText() == "hello\n"
