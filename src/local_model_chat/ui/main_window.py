from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from ..attachments import MAX_MESSAGE_BYTES, AttachmentError, load_attachment, render_attachments
from ..config import APP_NAME, MODEL_PRESETS, ModelPreset, export_dir
from ..context import calculate_budget, estimate_tokens
from ..domain import Attachment, GenerationMetrics, Message, Project
from ..molicoder.projectstate import read_git_state
from ..molicoder.registry import ProjectRegistry
from ..ollama import OllamaClient
from ..storage import ChatStore
from ..telemetry import format_bytes, read_nvidia_telemetry
from ..workers import ChatWorker, StartupWorker
from .widgets import AttachmentChip, ChatBubble, MessageComposer, MetricCard


class MainWindow(QMainWindow):
    def __init__(self, store: ChatStore | None = None, client: OllamaClient | None = None) -> None:
        super().__init__()
        self.store = store or ChatStore()
        self.registry = ProjectRegistry(self.store)
        self.client = client or OllamaClient()
        self.current_project_id: int | None = None
        self.current_conversation_id: int | None = None
        self.current_attachments: list[Attachment] = []
        self.chat_worker: ChatWorker | None = None
        self.startup_worker: StartupWorker | None = None
        self.active_bubble: ChatBubble | None = None
        self.last_metrics = GenerationMetrics()
        self._installed_model_names: set[str] = set()
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._perform_scroll_to_bottom)

        self.setWindowTitle(APP_NAME)
        self.resize(1420, 880)
        self.setMinimumSize(1020, 680)
        self._build_ui()
        self._build_menu()
        self._load_project_list(select_saved=True)
        self._load_conversation_list(select_first=True)
        self._start_ollama_check()

        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.timeout.connect(self._refresh_telemetry)
        self.telemetry_timer.start(3000)
        self._refresh_context_meter()

    @property
    def preset(self) -> ModelPreset:
        return MODEL_PRESETS[self.model_combo.currentData()]

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(18, 10, 18, 10)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        top_layout.addWidget(title)
        top_layout.addStretch()
        self.connection_label = QLabel("● Checking Ollama…")
        self.connection_label.setStyleSheet("color: #F59E0B; font-weight: 650;")
        top_layout.addWidget(self.connection_label)
        self.inspector_button = QPushButton("Context && performance")
        self.inspector_button.clicked.connect(self._toggle_inspector)
        top_layout.addWidget(self.inspector_button)
        root_layout.addWidget(topbar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.sidebar = self._build_sidebar()
        self.center = self._build_center()
        self.inspector = self._build_inspector()
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.center)
        self.splitter.addWidget(self.inspector)
        self.splitter.setSizes([245, 855, 320])
        root_layout.addWidget(self.splitter, 1)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(210)
        sidebar.setMaximumWidth(340)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 14, 12, 12)

        view_row = QHBoxLayout()
        self.chat_nav_button = QPushButton("Chat")
        self.chat_nav_button.setObjectName("primary")
        self.chat_nav_button.setCheckable(True)
        self.chat_nav_button.setChecked(True)
        self.chat_nav_button.clicked.connect(lambda: self._set_view("chat"))
        self.build_nav_button = QPushButton("Build")
        self.build_nav_button.setCheckable(True)
        self.build_nav_button.clicked.connect(lambda: self._set_view("build"))
        view_row.addWidget(self.chat_nav_button)
        view_row.addWidget(self.build_nav_button)
        layout.addLayout(view_row)

        projects_row = QHBoxLayout()
        projects_label = QLabel("PROJECTS")
        projects_label.setObjectName("sectionTitle")
        projects_row.addWidget(projects_label)
        projects_row.addStretch()
        add_project = QPushButton("＋ Add")
        add_project.setObjectName("ghost")
        add_project.clicked.connect(self._add_project)
        projects_row.addWidget(add_project)
        layout.addLayout(projects_row)
        self.project_list = QListWidget()
        self.project_list.setMaximumHeight(180)
        self.project_list.currentItemChanged.connect(self._project_selected)
        layout.addWidget(self.project_list)
        remove_project = QPushButton("Remove project")
        remove_project.setObjectName("ghost")
        remove_project.clicked.connect(self._remove_project)
        layout.addWidget(remove_project)

        new_button = QPushButton("＋  New chat")
        new_button.setObjectName("primary")
        new_button.clicked.connect(self._new_chat)
        layout.addWidget(new_button)
        chats_label = QLabel("CONVERSATIONS")
        chats_label.setObjectName("sectionTitle")
        layout.addWidget(chats_label)
        self.chat_list = QListWidget()
        self.chat_list.currentItemChanged.connect(self._conversation_selected)
        layout.addWidget(self.chat_list, 1)

        row = QHBoxLayout()
        rename = QPushButton("Rename")
        rename.setObjectName("ghost")
        rename.clicked.connect(self._rename_chat)
        delete = QPushButton("Delete")
        delete.setObjectName("ghost")
        delete.clicked.connect(self._delete_chat)
        row.addWidget(rename)
        row.addWidget(delete)
        layout.addLayout(row)
        return sidebar

    def _build_center(self) -> QWidget:
        self.view_stack = QStackedWidget()
        self.chat_view = self._build_chat_center()
        self.build_view = self._build_build_center()
        self.view_stack.addWidget(self.chat_view)
        self.view_stack.addWidget(self.build_view)
        return self.view_stack

    def _build_chat_center(self) -> QWidget:
        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(10)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model"))
        self.model_combo = QComboBox()
        for preset in MODEL_PRESETS.values():
            self.model_combo.addItem(f"{preset.label}  ·  {preset.role}", preset.name)
        default_index = self.model_combo.findData("molicoder:latest")
        self.model_combo.setCurrentIndex(max(0, default_index))
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        model_row.addWidget(self.model_combo)
        self.model_badge = QLabel("8,192 context")
        self.model_badge.setStyleSheet("color: #22D3EE; font-weight: 650;")
        model_row.addWidget(self.model_badge)
        model_row.addStretch()
        undo_button = QPushButton("Remove last exchange")
        undo_button.setObjectName("ghost")
        undo_button.clicked.connect(self._remove_last_exchange)
        model_row.addWidget(undo_button)
        layout.addLayout(model_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._scroll_range_changed)
        self.transcript = QWidget()
        self.transcript_layout = QVBoxLayout(self.transcript)
        self.transcript_layout.setContentsMargins(6, 8, 6, 8)
        self.transcript_layout.setSpacing(12)
        self.transcript_layout.addStretch()
        self.scroll.setWidget(self.transcript)
        layout.addWidget(self.scroll, 1)

        self.attachment_container = QWidget()
        self.attachment_layout = QHBoxLayout(self.attachment_container)
        self.attachment_layout.setContentsMargins(0, 0, 0, 0)
        self.attachment_layout.setSpacing(6)
        self.attachment_layout.addStretch()
        self.attachment_container.setVisible(False)
        layout.addWidget(self.attachment_container)

        composer_frame = QFrame()
        composer_frame.setObjectName("metricCard")
        composer_layout = QVBoxLayout(composer_frame)
        composer_layout.setContentsMargins(10, 9, 10, 9)
        self.composer = MessageComposer()
        self.composer.setPlaceholderText("Message your local model…  Enter to send · Shift+Enter for a new line. Drop text/code files here.")
        self.composer.setMinimumHeight(76)
        self.composer.setMaximumHeight(160)
        self.composer.textChanged.connect(self._refresh_context_meter)
        self.composer.files_dropped.connect(self._add_attachment_paths)
        self.composer.send_requested.connect(self._send_message)
        composer_layout.addWidget(self.composer)
        actions = QHBoxLayout()
        attach_button = QPushButton("Attach text/code")
        attach_button.clicked.connect(self._choose_attachments)
        actions.addWidget(attach_button)
        self.composer_hint = QLabel("Estimated context: —")
        self.composer_hint.setObjectName("muted")
        actions.addWidget(self.composer_hint)
        actions.addStretch()
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("danger")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._stop_generation)
        actions.addWidget(self.stop_button)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self._send_message)
        actions.addWidget(self.send_button)
        composer_layout.addLayout(actions)
        layout.addWidget(composer_frame)
        return center

    def _build_build_center(self) -> QWidget:
        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        heading = QLabel("BUILD / ORCHESTRATE")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.build_project_title = QLabel("No project selected")
        self.build_project_title.setStyleSheet("font-size: 24px; font-weight: 750; color: #F8FAFC;")
        layout.addWidget(self.build_project_title)
        self.build_project_meta = QLabel("Add a local project from the sidebar to begin.")
        self.build_project_meta.setObjectName("muted")
        self.build_project_meta.setWordWrap(True)
        layout.addWidget(self.build_project_meta)

        request_card = QFrame()
        request_card.setObjectName("metricCard")
        request_layout = QVBoxLayout(request_card)
        request_layout.addWidget(QLabel("Development request"))
        self.build_request = MessageComposer()
        self.build_request.setPlaceholderText(
            "Describe a small, explicit, mechanically verifiable task for this project…"
        )
        self.build_request.setMinimumHeight(120)
        request_layout.addWidget(self.build_request)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Mode"))
        self.build_mode = QComboBox()
        self.build_mode.addItems(["LOCAL_ONLY", "AUTO", "FRONTIER_PLAN", "MANUAL_PLAN"])
        controls.addWidget(self.build_mode)
        controls.addStretch()
        self.build_run_button = QPushButton("Run build")
        self.build_run_button.setObjectName("primary")
        self.build_run_button.setEnabled(False)
        self.build_run_button.setToolTip("Planning and run orchestration are introduced in Phase 2.")
        controls.addWidget(self.build_run_button)
        request_layout.addLayout(controls)
        layout.addWidget(request_card)

        foundation = QFrame()
        foundation.setObjectName("metricCard")
        foundation_layout = QVBoxLayout(foundation)
        foundation_title = QLabel("LOCAL EXECUTOR FOUNDATION READY")
        foundation_title.setObjectName("sectionTitle")
        foundation_layout.addWidget(foundation_title)
        foundation_text = QLabel(
            "The serialized Ollama lane, confined file tools, validation gate, and real-model "
            "executor proof are installed. Phase 2 connects reconnaissance, planning, and repair "
            "to this view."
        )
        foundation_text.setObjectName("muted")
        foundation_text.setWordWrap(True)
        foundation_layout.addWidget(foundation_text)
        layout.addWidget(foundation)
        layout.addStretch()
        return center

    def _build_inspector(self) -> QWidget:
        inspector = QFrame()
        inspector.setObjectName("inspector")
        inspector.setMinimumWidth(285)
        inspector.setMaximumWidth(400)
        layout = QVBoxLayout(inspector)
        layout.setContentsMargins(14, 14, 14, 14)
        title = QLabel("CONTEXT & PERFORMANCE")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.context_progress = QProgressBar()
        self.context_progress.setRange(0, 100)
        self.context_progress.setValue(0)
        layout.addWidget(self.context_progress)
        self.context_label = QLabel("0 estimated / 8,192 tokens")
        self.context_label.setObjectName("muted")
        self.context_label.setWordWrap(True)
        layout.addWidget(self.context_label)
        self.context_notice = QLabel("Pre-send values are conservative estimates. Ollama's exact count appears after generation.")
        self.context_notice.setObjectName("muted")
        self.context_notice.setWordWrap(True)
        layout.addWidget(self.context_notice)

        self.model_card = MetricCard("Model", "—")
        self.residency_card = MetricCard("Residency", "Not loaded")
        self.gpu_card = MetricCard("GPU memory", "—")
        self.speed_card = MetricCard("Generation", "—")
        self.tokens_card = MetricCard("Last response", "—")
        for card in (self.model_card, self.residency_card, self.gpu_card, self.speed_card, self.tokens_card):
            layout.addWidget(card)

        advanced_label = QLabel("ADVANCED GENERATION")
        advanced_label.setObjectName("sectionTitle")
        layout.addWidget(advanced_label)
        form = QFormLayout()
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.top_p_spin = QDoubleSpinBox()
        self.top_p_spin.setRange(0.05, 1.0)
        self.top_p_spin.setSingleStep(0.05)
        self.output_spin = QSpinBox()
        self.output_spin.setRange(64, 8192)
        self.output_spin.setSingleStep(256)
        for widget in (self.temperature_spin, self.top_p_spin, self.output_spin):
            widget.valueChanged.connect(self._refresh_context_meter)
        form.addRow("Temperature", self.temperature_spin)
        form.addRow("Top-p", self.top_p_spin)
        form.addRow("Max output", self.output_spin)
        layout.addLayout(form)

        export_row = QHBoxLayout()
        md_button = QPushButton("Export MD")
        md_button.clicked.connect(lambda: self._export_chat("md"))
        json_button = QPushButton("Export JSON")
        json_button.clicked.connect(lambda: self._export_chat("json"))
        export_row.addWidget(md_button)
        export_row.addWidget(json_button)
        layout.addLayout(export_row)
        layout.addStretch()
        clear_button = QPushButton("Clear all local chats")
        clear_button.setObjectName("danger")
        clear_button.clicked.connect(self._clear_all)
        layout.addWidget(clear_button)
        self._apply_preset_controls()
        return inspector

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        new_action = QAction("New chat", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self._new_chat)
        file_menu.addAction(new_action)
        export_action = QAction("Export Markdown", self)
        export_action.setShortcut("Ctrl+Shift+E")
        export_action.triggered.connect(lambda: self._export_chat("md"))
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def _start_ollama_check(self) -> None:
        self.startup_worker = StartupWorker(self.client)
        self.startup_worker.ready.connect(self._ollama_ready)
        self.startup_worker.finished.connect(
            lambda: self._startup_worker_finished(self.startup_worker)
        )
        self.startup_worker.start()

    def _ollama_ready(self, available: bool, models: list[dict]) -> None:
        self._installed_model_names = {str(model.get("name", "")) for model in models}
        if available:
            self.connection_label.setText("● Ollama connected")
            self.connection_label.setStyleSheet("color: #34D399; font-weight: 650;")
        else:
            self.connection_label.setText("● Ollama unavailable")
            self.connection_label.setStyleSheet("color: #FB7185; font-weight: 650;")
            QMessageBox.warning(
                self, "Ollama unavailable",
                "Local Model Chat could not reach Ollama at 127.0.0.1:11434. Start Ollama, then restart this app.",
            )
        self._update_model_availability()

    def _startup_worker_finished(self, worker: StartupWorker | None) -> None:
        if worker is None:
            return
        worker.deleteLater()
        if self.startup_worker is worker:
            self.startup_worker = None

    def _update_model_availability(self) -> None:
        model = self.preset.name
        missing = bool(self._installed_model_names) and model not in self._installed_model_names
        self.send_button.setEnabled(not missing and self.chat_worker is None)
        if missing:
            self.model_card.set_value(f"{self.preset.label}\nMissing from Ollama")
        else:
            self._refresh_model_card()

    def _refresh_model_card(self) -> None:
        try:
            details = self.client.show(self.preset.name)
            model_info = details.get("details", {})
            parameters = model_info.get("parameter_size", "—")
            quant = model_info.get("quantization_level", "—")
            self.model_card.set_value(f"{self.preset.label} · {parameters}\n{quant} · 8,192 tokens")
        except Exception:
            self.model_card.set_value(f"{self.preset.label}\n8,192 tokens")

    def _set_view(self, view: str) -> None:
        is_build = view == "build"
        self.view_stack.setCurrentWidget(self.build_view if is_build else self.chat_view)
        self.chat_nav_button.setChecked(not is_build)
        self.build_nav_button.setChecked(is_build)
        if is_build:
            self._update_build_project(self.registry.selected())

    def _load_project_list(self, *, select_saved: bool = False, select_id: int | None = None) -> None:
        projects = self.registry.list()
        selected = self.registry.selected() if select_saved else None
        target_id = select_id or (selected.id if selected else None)
        if target_id is None and projects:
            target_id = projects[0].id
        self.project_list.blockSignals(True)
        self.project_list.clear()
        selected_item = None
        for project in projects:
            item = QListWidgetItem(project.name)
            item.setData(Qt.ItemDataRole.UserRole, project.id)
            path = Path(project.path)
            state = read_git_state(path) if path.is_dir() else None
            state_text = (
                f"{state.branch} · {'dirty' if state.dirty else 'clean'}"
                if state and state.available
                else "Not a Git repository" if state else "Folder missing"
            )
            item.setToolTip(f"{project.path}\n{project.kind} · {state_text}")
            self.project_list.addItem(item)
            if project.id == target_id:
                selected_item = item
        self.project_list.blockSignals(False)
        if selected_item:
            self.project_list.setCurrentItem(selected_item)
            self._project_selected(selected_item, None)
        else:
            self.current_project_id = None
            self._update_build_project(None)

    def _project_selected(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        project_id = int(current.data(Qt.ItemDataRole.UserRole))
        try:
            project = self.registry.select(project_id)
        except ValueError:
            self._load_project_list(select_saved=True)
            return
        self.current_project_id = project.id
        self._update_build_project(project)

    def _update_build_project(self, project: Project | None) -> None:
        if project is None:
            self.build_project_title.setText("No project selected")
            self.build_project_meta.setText("Add a local project from the sidebar to begin.")
            self.build_request.setEnabled(False)
            return
        path = Path(project.path)
        self.build_project_title.setText(project.name)
        if not path.is_dir():
            self.build_project_meta.setText(f"{project.kind.title()} · Folder missing\n{project.path}")
            self.build_request.setEnabled(False)
            return
        git_state = read_git_state(path)
        repository = (
            f"{git_state.branch} · {'dirty working tree' if git_state.dirty else 'clean working tree'}"
            if git_state.available
            else "Not a Git repository"
        )
        self.build_project_meta.setText(
            f"{project.kind.title()} · {repository}\n{project.path}"
        )
        self.build_request.setEnabled(True)

    def _add_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add local project")
        if not path:
            return
        try:
            self._add_project_path(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not add project", str(exc))

    def _add_project_path(self, path: str | Path) -> Project:
        project = self.registry.add(path)
        self._load_project_list(select_id=project.id)
        return project

    def _remove_project(self) -> None:
        item = self.project_list.currentItem()
        if item is None:
            return
        project = self.registry.get(int(item.data(Qt.ItemDataRole.UserRole)))
        if project is None:
            return
        if QMessageBox.question(
            self,
            "Remove project",
            f"Remove {project.name} from MoliCoder? The project folder and .molicoder data stay on disk.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.registry.remove(project.id)
        self.current_project_id = None
        self._load_project_list(select_saved=True)

    def _load_conversation_list(self, select_first: bool = False, select_id: int | None = None) -> None:
        self.chat_list.blockSignals(True)
        self.chat_list.clear()
        selected_item = None
        for conversation in self.store.list_conversations():
            item = QListWidgetItem(conversation.title)
            item.setData(Qt.ItemDataRole.UserRole, conversation.id)
            item.setToolTip(f"{conversation.model}\nUpdated {conversation.updated_at}")
            self.chat_list.addItem(item)
            if select_id == conversation.id:
                selected_item = item
        self.chat_list.blockSignals(False)
        if selected_item:
            self.chat_list.setCurrentItem(selected_item)
            self._conversation_selected(selected_item, None)
        elif select_first and self.chat_list.count():
            self.chat_list.setCurrentRow(0)
        elif not self.chat_list.count():
            self._new_chat()

    def _new_chat(self, model: str | None = None) -> None:
        if self.chat_worker:
            self._stop_generation()
        model = model or self.model_combo.currentData()
        conversation = self.store.create_conversation(model)
        self._load_conversation_list(select_id=conversation.id)
        self.composer.setFocus()

    def _conversation_selected(self, current: QListWidgetItem | None, _previous) -> None:
        if not current:
            return
        conversation_id = int(current.data(Qt.ItemDataRole.UserRole))
        conversation = self.store.get_conversation(conversation_id)
        if not conversation:
            return
        self.current_conversation_id = conversation.id
        index = self.model_combo.findData(conversation.model)
        if index >= 0:
            self.model_combo.blockSignals(True)
            self.model_combo.setCurrentIndex(index)
            self.model_combo.blockSignals(False)
            self._apply_preset_controls()
        self._render_messages(self.store.list_messages(conversation.id))
        self._refresh_context_meter()

    def _render_messages(self, messages: list[Message]) -> None:
        while self.transcript_layout.count() > 1:
            item = self.transcript_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for message in messages:
            if message.role in {"user", "assistant"}:
                self.transcript_layout.insertWidget(
                    self.transcript_layout.count() - 1,
                    ChatBubble(message.role, message.content, message.thinking),
                )
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _model_changed(self) -> None:
        self._apply_preset_controls()
        self._update_model_availability()
        if self.current_conversation_id:
            conversation = self.store.get_conversation(self.current_conversation_id)
            if conversation and conversation.model != self.preset.name:
                try:
                    self.client.unload(conversation.model)
                except Exception:
                    pass
                self._new_chat(self.preset.name)

    def _apply_preset_controls(self) -> None:
        preset = self.preset
        self.temperature_spin.blockSignals(True)
        self.top_p_spin.blockSignals(True)
        self.output_spin.blockSignals(True)
        self.temperature_spin.setValue(preset.temperature)
        self.top_p_spin.setValue(preset.top_p)
        self.output_spin.setValue(preset.max_output_tokens)
        self.temperature_spin.blockSignals(False)
        self.top_p_spin.blockSignals(False)
        self.output_spin.blockSignals(False)
        self.model_badge.setText(f"{preset.context_window:,} context")
        self.model_badge.setStyleSheet(f"color: {preset.accent}; font-weight: 650;")
        self._refresh_context_meter()

    def _current_messages(self) -> list[Message]:
        if self.current_conversation_id is None:
            return []
        return self.store.list_messages(self.current_conversation_id)

    def _pending_content(self) -> str:
        return self.composer.toPlainText().strip() + render_attachments(self.current_attachments)

    def _refresh_context_meter(self) -> None:
        if not hasattr(self, "context_progress"):
            return
        messages = self._current_messages()
        reserved = int(self.output_spin.value()) if hasattr(self, "output_spin") else self.preset.max_output_tokens
        budget = calculate_budget(messages, self._pending_content(), self.preset.context_window, reserved)
        percent = round(budget.fraction * 100)
        self.context_progress.setValue(percent)
        color = "#FB7185" if budget.blocked else "#F59E0B" if budget.warning else self.preset.accent
        self.context_progress.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}")
        self.context_label.setText(
            f"{budget.estimated_prompt_tokens:,} estimated prompt + {reserved:,} reserved output\n"
            f"{budget.remaining:,} safe tokens remaining · {percent}%"
        )
        self.composer_hint.setText(f"~{estimate_tokens(self._pending_content()):,} tokens in pending message")
        can_send = bool(self.composer.toPlainText().strip() or self.current_attachments) and not budget.blocked
        can_send = can_send and self.chat_worker is None
        if self._installed_model_names:
            can_send = can_send and self.preset.name in self._installed_model_names
        self.send_button.setEnabled(can_send)
        if budget.blocked:
            self.context_notice.setText("Safe context limit exceeded. Start a new chat or remove the last exchange; history will not be trimmed silently.")
            self.context_notice.setStyleSheet("color: #FB7185; font-weight: 650;")
        elif budget.warning:
            self.context_notice.setText("Context is nearly full. Consider a new chat before sending more large messages.")
            self.context_notice.setStyleSheet("color: #F59E0B; font-weight: 650;")
        else:
            self.context_notice.setText("Pre-send values are conservative estimates. Ollama's exact count appears after generation.")
            self.context_notice.setStyleSheet("color: #94A3B8;")

    def _send_message(self) -> None:
        if self.chat_worker or self.current_conversation_id is None:
            return
        content = self._pending_content()
        if not content.strip():
            return
        messages_before = self._current_messages()
        budget = calculate_budget(
            messages_before, content, self.preset.context_window, int(self.output_spin.value())
        )
        if budget.blocked:
            QMessageBox.warning(
                self, "Context limit reached",
                "This request would exceed the safe 95% context budget. Start a new chat or remove the last exchange.",
            )
            return
        user_message = Message(None, self.current_conversation_id, "user", content)
        self.store.add_message(user_message)
        conversation = self.store.get_conversation(self.current_conversation_id)
        message_rendered_by_refresh = False
        if conversation and conversation.title == "New chat":
            title = " ".join(self.composer.toPlainText().strip().split())[:56] or "Attached files"
            self.store.rename_conversation(conversation.id, title)
            self._load_conversation_list(select_id=conversation.id)
            message_rendered_by_refresh = True

        if not message_rendered_by_refresh:
            self.transcript_layout.insertWidget(
                self.transcript_layout.count() - 1, ChatBubble("user", content)
            )
        self.active_bubble = ChatBubble("assistant")
        self.active_bubble.reasoning_frame.setVisible(self.preset.show_thinking)
        self.transcript_layout.insertWidget(self.transcript_layout.count() - 1, self.active_bubble)
        self.composer.clear()
        self.current_attachments.clear()
        self._render_attachment_chips()
        self._scroll_to_bottom()

        api_messages = [{"role": "system", "content": self.preset.system_prompt}]
        for message in self.store.list_messages(self.current_conversation_id):
            if message.role in {"user", "assistant"}:
                api_messages.append({"role": message.role, "content": message.content})
        options = {
            "context_window": self.preset.context_window,
            "max_output_tokens": int(self.output_spin.value()),
            "temperature": float(self.temperature_spin.value()),
            "top_p": float(self.top_p_spin.value()),
            "show_thinking": self.preset.show_thinking,
            "keep_alive": "5m",
        }
        self.chat_worker = ChatWorker(self.client, self.preset.name, api_messages, options)
        self.chat_worker.content_delta.connect(self._append_content)
        self.chat_worker.thinking_delta.connect(self._append_thinking)
        self.chat_worker.completed.connect(self._generation_finished)
        self.chat_worker.failed.connect(self._generation_failed)
        self.chat_worker.start()
        self.send_button.setVisible(False)
        self.stop_button.setVisible(True)
        self.model_combo.setEnabled(False)
        self._refresh_context_meter()

    def _append_content(self, delta: str) -> None:
        if self.active_bubble:
            self.active_bubble.append_content(delta)
            self._scroll_to_bottom()

    def _append_thinking(self, delta: str) -> None:
        if self.active_bubble:
            self.active_bubble.append_thinking(delta)
            self._scroll_to_bottom()

    def _generation_finished(self, metrics: GenerationMetrics) -> None:
        self.last_metrics = metrics
        if self.active_bubble and self.current_conversation_id is not None:
            content = self.active_bubble.content
            thinking = self.active_bubble.thinking
            if not content:
                if metrics.done_reason == "cancelled":
                    content = "_Generation stopped before a final answer was produced._"
                else:
                    content = "_The model reached its output limit without producing a final answer. Partial reasoning was preserved._"
                self.active_bubble.set_content(content)
            self.active_bubble.mark_complete()
            message = Message(
                None, self.current_conversation_id, "assistant", content, thinking,
                metrics.prompt_tokens or None, metrics.output_tokens or None,
            )
            self.store.add_message(message, metrics)
        self._reset_generation_state()
        self._update_metrics_cards()
        self._load_conversation_list(select_id=self.current_conversation_id)

    def _generation_failed(self, error: str) -> None:
        if self.active_bubble:
            self.active_bubble.set_content(f"**Generation failed:** {error}")
        self._reset_generation_state()
        QMessageBox.critical(self, "Generation failed", error)

    def _reset_generation_state(self) -> None:
        worker = self.chat_worker
        self.chat_worker = None
        self.active_bubble = None
        self.stop_button.setVisible(False)
        self.send_button.setVisible(True)
        self.model_combo.setEnabled(True)
        self._refresh_context_meter()
        if worker:
            worker.deleteLater()

    def _stop_generation(self) -> None:
        if self.chat_worker:
            self.chat_worker.cancel()
            self.stop_button.setEnabled(False)
            self.stop_button.setText("Stopping…")
            QTimer.singleShot(2500, lambda: (self.stop_button.setEnabled(True), self.stop_button.setText("Stop")))

    def _scroll_to_bottom(self) -> None:
        self.transcript_layout.activate()
        self._scroll_timer.start(0)

    def _perform_scroll_to_bottom(self) -> None:
        self.transcript_layout.activate()
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        self.scroll.verticalScrollBar().setValue(maximum)

    def _choose_attachments(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach text or code files")
        self._add_attachment_paths(paths)

    def _add_attachment_paths(self, paths: list[str]) -> None:
        for path in paths:
            if any(str(item.path) == str(Path(path).resolve()) for item in self.current_attachments):
                continue
            try:
                attachment = load_attachment(path)
                if sum(item.size for item in self.current_attachments) + attachment.size > MAX_MESSAGE_BYTES:
                    raise AttachmentError("Attachments exceed the 4 MB per-message limit")
                self.current_attachments.append(attachment)
            except AttachmentError as exc:
                QMessageBox.warning(self, "Attachment rejected", str(exc))
        self._render_attachment_chips()
        self._refresh_context_meter()

    def _render_attachment_chips(self) -> None:
        while self.attachment_layout.count() > 1:
            item = self.attachment_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for attachment in self.current_attachments:
            chip = AttachmentChip(str(attachment.path), attachment.name, format_bytes(attachment.size))
            chip.removed.connect(self._remove_attachment)
            self.attachment_layout.insertWidget(self.attachment_layout.count() - 1, chip)
        self.attachment_container.setVisible(bool(self.current_attachments))

    def _remove_attachment(self, path: str) -> None:
        self.current_attachments = [item for item in self.current_attachments if str(item.path) != path]
        self._render_attachment_chips()
        self._refresh_context_meter()

    def _refresh_telemetry(self) -> None:
        telemetry = read_nvidia_telemetry()
        if telemetry:
            self.gpu_card.set_value(
                f"{telemetry.used_mib:,} / {telemetry.total_mib:,} MiB\n"
                f"{telemetry.utilization_percent}% GPU · {telemetry.free_mib:,} MiB free"
            )
        else:
            self.gpu_card.set_value("NVIDIA telemetry unavailable")
        try:
            running = self.client.running_models()
            match = next((item for item in running if item.get("name") in {self.preset.name, self.preset.name.removesuffix(":latest")}), None)
            if match:
                size = int(match.get("size", 0))
                vram = int(match.get("size_vram", 0))
                gpu_percent = round(vram / size * 100) if size else 0
                self.residency_card.set_value(
                    f"{gpu_percent}% GPU / {100 - gpu_percent}% CPU\n{format_bytes(size)} · {match.get('context_length', 0):,} context"
                )
            else:
                self.residency_card.set_value("Not loaded")
        except Exception:
            self.residency_card.set_value("Ollama unavailable")

    def _update_metrics_cards(self) -> None:
        metrics = self.last_metrics
        self.speed_card.set_value(
            f"{metrics.output_tokens_per_second:.1f} tok/s output\n"
            f"{metrics.prompt_tokens_per_second:.1f} tok/s prompt"
        )
        self.tokens_card.set_value(
            f"{metrics.prompt_tokens:,} prompt · {metrics.output_tokens:,} output\n"
            f"{metrics.total_seconds:.1f}s total · {metrics.load_seconds:.1f}s load"
        )
        self.context_notice.setText(
            f"Actual last prompt: {metrics.prompt_tokens:,} tokens. Pre-send values remain estimates for the next request."
        )

    def _rename_chat(self) -> None:
        if self.current_conversation_id is None:
            return
        conversation = self.store.get_conversation(self.current_conversation_id)
        if not conversation:
            return
        title, ok = QInputDialog.getText(self, "Rename conversation", "Title", text=conversation.title)
        if ok:
            self.store.rename_conversation(conversation.id, title)
            self._load_conversation_list(select_id=conversation.id)

    def _delete_chat(self) -> None:
        if self.current_conversation_id is None:
            return
        if QMessageBox.question(self, "Delete conversation", "Delete this local conversation permanently?") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_conversation(self.current_conversation_id)
        self.current_conversation_id = None
        self._load_conversation_list(select_first=True)

    def _clear_all(self) -> None:
        if QMessageBox.question(
            self, "Clear all chats", "Delete every locally saved conversation? This cannot be undone."
        ) != QMessageBox.StandardButton.Yes:
            return
        self.store.clear_all()
        self.current_conversation_id = None
        self._load_conversation_list(select_first=True)

    def _remove_last_exchange(self) -> None:
        if self.current_conversation_id is None or not self._current_messages():
            return
        self.store.delete_last_exchange(self.current_conversation_id)
        self._render_messages(self._current_messages())
        self._refresh_context_meter()

    def _export_chat(self, kind: str) -> None:
        if self.current_conversation_id is None:
            return
        conversation = self.store.get_conversation(self.current_conversation_id)
        if not conversation:
            return
        safe_title = "".join(character if character.isalnum() or character in "-_ " else "" for character in conversation.title).strip()
        suffix = ".md" if kind == "md" else ".json"
        suggested = export_dir() / f"{safe_title or 'conversation'}{suffix}"
        destination, _ = QFileDialog.getSaveFileName(self, "Export conversation", str(suggested), f"*{suffix}")
        if not destination:
            return
        try:
            if kind == "md":
                self.store.export_markdown(conversation.id, Path(destination))
            else:
                self.store.export_json(conversation.id, Path(destination))
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def _toggle_inspector(self) -> None:
        self.inspector.setVisible(not self.inspector.isVisible())

    def closeEvent(self, event) -> None:
        if self.chat_worker:
            self.chat_worker.cancel()
            self.chat_worker.wait(2500)
        if self.startup_worker and self.startup_worker.isRunning():
            self.startup_worker.wait(3000)
        event.accept()
