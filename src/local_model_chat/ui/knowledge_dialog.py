from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QTextBrowser, QVBoxLayout,
)

from ..knowledge import ProjectKnowledge
from ..storage import ChatStore


class KnowledgeReviewDialog(QDialog):
    def __init__(self, store: ChatStore, project_id: int, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.project_id = project_id
        self.knowledge = ProjectKnowledge(store)
        self.setWindowTitle("Project knowledge")
        self.resize(690, 480)
        layout = QVBoxLayout(self)
        project = store.get_project(project_id)
        layout.addWidget(QLabel(f"Project knowledge · {project.name if project else project_id}"))
        self.items = QListWidget()
        self.items.currentItemChanged.connect(self._show_selected)
        layout.addWidget(self.items, 1)
        self.details = QTextBrowser()
        layout.addWidget(self.details, 2)
        actions = QHBoxLayout()
        self.approve_button = QPushButton("Approve decision")
        self.approve_button.clicked.connect(self._approve)
        actions.addWidget(self.approve_button)
        self.correct_button = QPushButton("Correct decision")
        self.correct_button.clicked.connect(self._correct)
        actions.addWidget(self.correct_button)
        self.invalidate_button = QPushButton("Invalidate")
        self.invalidate_button.clicked.connect(self._invalidate)
        actions.addWidget(self.invalidate_button)
        self.archive_button = QPushButton("Archive")
        self.archive_button.clicked.connect(self._archive)
        actions.addWidget(self.archive_button)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self._reload()

    def _selected_id(self) -> int | None:
        item = self.items.currentItem()
        return int(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _reload(self, select_id: int | None = None) -> None:
        self.items.clear()
        target = None
        for record in self.store.list_project_knowledge(self.project_id):
            item = QListWidgetItem(f"#{record.id} · {record.kind} · {record.status} · {record.title}")
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            self.items.addItem(item)
            if record.id == select_id:
                target = item
        if target:
            self.items.setCurrentItem(target)
        elif self.items.count():
            self.items.setCurrentRow(0)
        else:
            self._show_selected()

    def _show_selected(self, *_args) -> None:
        item_id = self._selected_id()
        record = self.store.get_knowledge_item(item_id) if item_id is not None else None
        if record is None:
            self.details.setPlainText("No knowledge item selected.")
            for button in (self.approve_button, self.correct_button, self.invalidate_button, self.archive_button):
                button.setEnabled(False)
            return
        approved = self.knowledge.approved_revision(record.id) if record.kind == "decision" else None
        lines = [
            f"{record.title} (#{record.id}, revision {record.revision}, {record.status})",
            "", record.body, "", f"Rationale: {record.rationale or 'not recorded'}",
            f"Author: {record.author}", f"Confidence: {record.confidence}",
            f"Source: {record.source_ref or 'not recorded'}",
            f"Source revision: {record.source_revision or 'not recorded'}",
            f"Updated: {record.updated_at}",
        ]
        if approved and approved.revision != record.revision:
            lines.extend(("", f"Previously approved revision {approved.revision}: {approved.body}"))
        self.details.setPlainText("\n".join(lines))
        editable = record.status not in {"superseded", "invalidated", "archived"}
        self.approve_button.setEnabled(record.kind == "decision" and editable and record.status != "approved")
        self.correct_button.setEnabled(record.kind == "decision" and editable)
        self.invalidate_button.setEnabled(editable)
        self.archive_button.setEnabled(editable)

    def _approve(self) -> None:
        item_id = self._selected_id()
        if item_id is None:
            return
        self.knowledge.approve_decision(item_id, actor="user")
        self._reload(item_id)

    def _correct(self) -> None:
        item_id = self._selected_id()
        if item_id is None:
            return
        current = self.store.get_knowledge_item(item_id)
        if current is None or current.kind != "decision":
            return
        title, ok = QInputDialog.getText(self, "Correct decision", "Title", text=current.title)
        if not ok:
            return
        body, ok = QInputDialog.getMultiLineText(self, "Correct decision", "Decision", current.body)
        if not ok:
            return
        rationale, ok = QInputDialog.getMultiLineText(self, "Correct decision", "Rationale", current.rationale)
        if not ok:
            return
        if not title.strip() or not body.strip() or not rationale.strip():
            QMessageBox.warning(self, "Incomplete decision", "Title, decision, and rationale are required.")
            return
        self.knowledge.correct_decision(
            item_id, title=title, body=body, rationale=rationale, author="user",
            source_ref="user correction in Knowledge UI", source_revision="",
        )
        self._reload(item_id)

    def _invalidate(self) -> None:
        item_id = self._selected_id()
        if item_id is not None and QMessageBox.question(
            self, "Invalidate knowledge", "Invalidate this knowledge item? Its history remains available."
        ) == QMessageBox.StandardButton.Yes:
            self.knowledge.invalidate(item_id, actor="user")
            self._reload(item_id)

    def _archive(self) -> None:
        item_id = self._selected_id()
        if item_id is not None and QMessageBox.question(
            self, "Archive knowledge", "Archive this knowledge item? Its history remains available."
        ) == QMessageBox.StandardButton.Yes:
            self.knowledge.archive(item_id, actor="user")
            self._reload(item_id)
