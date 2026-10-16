from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTextBrowser,
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
        header.addStretch()
        copy_button = QPushButton("Copy")
        copy_button.setObjectName("ghost")
        copy_button.setMinimumHeight(copy_button.sizeHint().height())
        copy_button.clicked.connect(self.copy_content)
        header.addWidget(copy_button)
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
        self.body.setStyleSheet("background: transparent; border: none; padding: 0;")
        self.apply_theme()
        self.body.document().documentLayout().documentSizeChanged.connect(self._resize_body)
        outer.addWidget(self.body)
        self.set_content(content)
        self.set_thinking(thinking)

    def apply_theme(self) -> None:
        from PySide6.QtWidgets import QApplication
        light = QApplication.instance().property("molicoderTheme") == "light"
        background, border, foreground = ("#E7EEE3", "#216442", "#202C23") if light else ("#001006", "#00EE24", "#85FF9C")
        self.body.document().setDefaultStyleSheet(
            f"blockquote {{ background-color: {background}; border-left: 4px solid {border}; "
            f"margin: 8px 0; padding: 10px 12px; color: {foreground}; }}"
        )
        # Reparse only this displayed document; stored content/history is untouched.
        if self.content:
            self._set_body_markdown(normalize_model_markdown(self.content))

    def _resize_body(self, size) -> None:
        self.body.setFixedHeight(max(28, int(size.height()) + 8))

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
