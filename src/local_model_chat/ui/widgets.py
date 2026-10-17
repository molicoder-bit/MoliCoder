from __future__ import annotations

import math
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTextBrowser,
    QToolButton, QVBoxLayout, QWidget, QTextEdit,
)

from ..formatting import normalize_model_markdown


class ChatBubble(QFrame):
    def __init__(self, role: str, content: str = "", thinking: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self.content = content
        self.thinking = thinking
        self.setObjectName("userBubble" if role == "user" else "assistantBubble")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)

        header = QHBoxLayout()
        author = QLabel("You" if role == "user" else "MoliCoder")
        author.setObjectName("messageAuthor")
        header.addWidget(author)
        self.origin_label = QLabel()
        self.origin_label.setObjectName('muted')
        self.origin_label.setTextFormat(Qt.PlainText)
        self.origin_label.setWordWrap(True)
        self.origin_label.setMinimumWidth(0)
        self.origin_label.hide()
        header.addWidget(self.origin_label, 1)
        self.elapsed_label = QLabel()
        self.elapsed_label.setObjectName('muted')
        self.elapsed_label.hide()
        header.addWidget(self.elapsed_label)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setObjectName("ghost")
        self.copy_button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.copy_button.clicked.connect(self.copy_content)
        header.addWidget(self.copy_button)
        outer.addLayout(header)

        self.reasoning_frame = QFrame()
        self.reasoning_frame.setObjectName("reasoningPanel")
        reasoning_layout = QVBoxLayout(self.reasoning_frame)
        reasoning_layout.setContentsMargins(9, 6, 9, 8)
        self.reasoning_toggle = QToolButton()
        self.reasoning_toggle.setText("Reasoning")
        self.reasoning_toggle.setCheckable(True)
        self.reasoning_toggle.setChecked(False)
        self.reasoning_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.reasoning_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.reasoning_toggle.toggled.connect(self._toggle_reasoning)
        reasoning_layout.addWidget(self.reasoning_toggle)
        self.reasoning_view = QTextBrowser()
        self.reasoning_view.setOpenExternalLinks(False)
        self.reasoning_view.setMaximumHeight(220)
        self.reasoning_view.setVisible(False)
        reasoning_layout.addWidget(self.reasoning_view)
        outer.addWidget(self.reasoning_frame)

        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.body.setStyleSheet("QTextBrowser { background: transparent; border: none; padding: 0; }")
        self.body.document().setDocumentMargin(0)
        self.apply_theme()
        self.body.document().documentLayout().documentSizeChanged.connect(self._resize_body)
        outer.addWidget(self.body)
        self.set_content(content)
        self.set_thinking(thinking)
        self.set_compact(QApplication.instance().property('molicoderCompactChat') is not False)

    def set_compact(self, compact: bool) -> None:
        margins = (10, 6, 10, 6) if compact else (14, 10, 14, 10)
        self.layout().setContentsMargins(*margins)
        self.layout().setSpacing(4 if compact else 8)
        self.copy_button.setStyleSheet('padding: 2px 6px;' if compact else '')

    def set_reply_details(self, origin: str, seconds=None) -> None:
        self.origin_label.setText(origin)
        self.origin_label.setToolTip(origin)
        self.origin_label.setVisible(bool(origin))
        valid = isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds) and seconds >= 0
        self.elapsed_label.setVisible(valid)
        if valid:
            minutes, remainder = divmod(round(seconds), 60)
            duration = '<0.1s' if seconds < 0.1 else f'{seconds:.1f}s' if seconds < 60 else f'{minutes}m {remainder}s'
            self.elapsed_label.setText(duration)
            self.elapsed_label.setAccessibleName(f'Response time: {duration}')
            self.elapsed_label.setToolTip('Time from sending to the completed reply, including evaluation, tools, fallbacks and any approval wait.')

    def apply_theme(self) -> None:
        from PySide6.QtWidgets import QApplication
        from .styles import theme_tokens
        t = theme_tokens(QApplication.instance().property('molicoderTheme'))
        self.body.document().setDefaultStyleSheet(
            f"blockquote {{ background-color: {t['code']}; border-left: 4px solid {t['accent']}; "
            f"margin: 8px 0; padding: 10px 12px; color: {t['text']}; }}"
            f"pre, code {{ background-color: {t['code']}; color: {t['text']}; }}"
            f"a {{ color: {t['accent']}; }}"
        )
        self.body.document().setDefaultFont(self.body.font())
        # Reparse only this displayed document; stored content/history is untouched.
        if self.content:
            self._set_body_markdown(normalize_model_markdown(self.content))

    def _resize_body(self, size) -> None:
        self.body.setFixedHeight(max(self.body.fontMetrics().height() + 2, math.ceil(size.height()) + 2))

    def _toggle_reasoning(self, checked: bool) -> None:
        self.reasoning_view.setVisible(checked)
        self.reasoning_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def set_content(self, content: str) -> None:
        self.content = content
        self._set_body_markdown(normalize_model_markdown(content) if content else "_Generating…_")

    def _set_body_markdown(self, markdown: str) -> None:
        previous = self.body.textCursor()
        selection = (previous.anchor(), previous.position()) if previous.hasSelection() else None
        self.body.setMarkdown(markdown)
        if selection:
            end = self.body.document().characterCount() - 1
            cursor = self.body.textCursor()
            cursor.setPosition(min(selection[0], end))
            cursor.setPosition(min(selection[1], end), QTextCursor.KeepAnchor)
            self.body.setTextCursor(cursor)

    def append_content(self, delta: str) -> None:
        self.set_content(self.content + delta)

    def set_thinking(self, thinking: str) -> None:
        self.thinking = thinking
        self.reasoning_frame.setVisible(bool(thinking))
        self.reasoning_view.setPlainText(thinking)
        if thinking:
            self.reasoning_toggle.setText("Reasoning · streaming" if not self.content else "Reasoning")

    def append_thinking(self, delta: str) -> None:
        self.set_thinking(self.thinking + delta)

    def mark_complete(self) -> None:
        if self.thinking:
            self.reasoning_toggle.setText("Reasoning")

    def copy_content(self) -> None:
        text = self.content
        if self.thinking:
            text = f"Reasoning:\n{self.thinking}\n\nAnswer:\n{self.content}"
        QGuiApplication.clipboard().setText(text)


class AttachmentChip(QFrame):
    removed = Signal(str)

    def __init__(self, path: str, name: str, size_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.setObjectName("attachmentChip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        label = QLabel(f"{name} · {size_label}")
        layout.addWidget(label)
        button = QPushButton("×")
        button.setObjectName("ghost")
        button.setFixedSize(26, 24)
        button.clicked.connect(lambda: self.removed.emit(self.path))
        layout.addWidget(button)


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 9, 11, 9)
        layout.setSpacing(3)
        title_label = QLabel(title.upper())
        title_label.setObjectName("sectionTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        self.value_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class MessageComposer(QTextEdit):
    files_dropped = Signal(list)
    send_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._compact = True
        self.document().documentLayout().documentSizeChanged.connect(lambda _: self._resize_to_draft())
        self.textChanged.connect(self._resize_to_draft)

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self._resize_to_draft()

    def _resize_to_draft(self) -> None:
        minimum = 56 if self._compact else 76
        self.setFixedHeight(max(minimum, min(160, math.ceil(self.document().size().height()) + 22)))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def keyPressEvent(self, event) -> None:
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        is_line_break = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if is_enter and not is_line_break:
            self.send_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)
