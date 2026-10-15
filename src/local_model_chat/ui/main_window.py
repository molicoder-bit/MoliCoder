from __future__ import annotations

import json
import hashlib
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QDir, QFile, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSplitter, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ..attachments import MAX_MESSAGE_BYTES, AttachmentError, load_attachment, render_attachments
from ..authorization import ActionRequest, ActionRisk, AuthorizationPolicy
from ..config import APP_NAME, MODEL_PRESETS, ModelPreset, data_dir, export_dir
from ..controller import ControllerResult, MainController
from ..environment.auth import CredentialUnavailable, ProtectedCredentialStore, store_provider_key
from ..environment.security import platform_security
from ..context import calculate_budget, estimate_tokens
from ..domain import Attachment, GenerationMetrics, Message, Project
from ..inference.adapters.ollama import ensure_local_offering, local_offering_id
from ..inference.api_dispatch import preview_custom_chat
from ..inference.onboarding import (
    API_PRESETS, CustomRoute, CustomRouteFacts, attest_custom_route_facts,
    check_custom_connection, is_api_offering, register_custom_route, register_preset_route,
)
from ..inference.policy import InferencePolicy
from ..inference.pacing import project_pacing
from ..inference.registry import ProviderRegistry
from ..kandev.ui_config import configured_kandev_adapter, kandev_board_url
from ..local_actions import requested_folder
from ..molicoder.projectstate import read_git_state
from ..molicoder.registry import ProjectRegistry
from ..molicoder.runtime import build_orchestrator
from ..molicoder.metrics import RunAnalytics
from ..molicoder.providers import AGENT_PROFILES, ProviderUnavailable, agent_profile, create_provider, probe_all_providers
from ..ollama import OllamaClient
from ..storage import ChatStore
from ..telemetry import format_bytes, read_nvidia_telemetry
from ..workers import (
    ChatWorker, CustomApiWorker, DoctorWorker, ManagedInstallWorker,
    ManagedRepairWorker, ModelInspectWorker, ModelPullWorker, OrchestrationWorker, StartupWorker,
    WindowsOllamaInstallWorker,
)
from .knowledge_dialog import KnowledgeReviewDialog
from .widgets import AttachmentChip, ChatBubble, MessageComposer, MetricCard


class MainWindow(QMainWindow):
    def __init__(
        self, store: ChatStore | None = None, client: OllamaClient | None = None,
        *, start_ollama_check: bool = True,
    ) -> None:
        super().__init__()
        self.store = store or ChatStore()
        self.store.recover_interrupted_runs()
        self.controller = MainController(self.store)
        self.inference_registry = ProviderRegistry(self.store)
        self.inference_policy = InferencePolicy(self.inference_registry)
        for preset in MODEL_PRESETS.values():
            ensure_local_offering(
                self.inference_registry, preset.name,
                context_window=preset.context_window,
                max_output_tokens=preset.max_output_tokens,
            )
        self.registry = ProjectRegistry(self.store)
        self.client = client or OllamaClient()
        self.current_project_id: int | None = None
        self.pending_build_request: tuple[int | None, str] | None = None
        self.current_conversation_id: int | None = None
        self.current_attachments: list[Attachment] = []
        self.managed_install_worker: ManagedInstallWorker | None = None
        self.managed_repair_worker: ManagedRepairWorker | None = None
        self.model_inspect_worker: ModelInspectWorker | None = None
        self.model_pull_worker: ModelPullWorker | None = None
        self.ollama_install_worker: WindowsOllamaInstallWorker | None = None
        self.custom_api_worker: CustomApiWorker | None = None
        self.chat_worker: ChatWorker | None = None
        self.startup_worker: StartupWorker | None = None
        self.doctor_worker: DoctorWorker | None = None
        self.doctor_dialog: QDialog | None = None
        self.orchestration_worker: OrchestrationWorker | None = None
        self.active_bubble: ChatBubble | None = None
        self.last_metrics = GenerationMetrics()
        self.last_integration_branch: str | None = None
        self.last_integration_path: Path | None = None
        self.last_run_log_path: Path | None = None
        self.recoverable_run_id: int | None = None
        self.last_run_id: int | None = None
        self._installed_model_names: set[str] = set()
        self._installed_model_presets: dict[str, ModelPreset] = {}
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._perform_scroll_to_bottom)
        self._build_started_at = 0.0
        self._build_activity_frame = 0
        self._build_activity_timer = QTimer(self)
        self._build_activity_timer.setInterval(1000)
        self._build_activity_timer.timeout.connect(self._update_build_activity)

        self.setWindowTitle(APP_NAME)
        self.resize(1420, 880)
        self.setMinimumSize(1020, 680)
        self._build_ui()
        self._build_menu()
        self._load_project_list(select_saved=True)
        self._load_conversation_list(select_first=True)
        if start_ollama_check:
            self._start_ollama_check()

        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.timeout.connect(self._refresh_telemetry)
        self.telemetry_timer.start(3000)
        self._refresh_context_meter()

    @property
    def preset(self) -> ModelPreset:
        name = self.model_combo.currentData()
        return MODEL_PRESETS.get(name) or self._installed_model_presets[name]

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
        new_project = QPushButton("＋ New")
        new_project.setObjectName("primary")
        new_project.clicked.connect(self._create_project)
        projects_row.addWidget(new_project)
        add_project = QPushButton("Add")
        add_project.setObjectName("ghost")
        add_project.clicked.connect(self._add_project)
        projects_row.addWidget(add_project)
        layout.addLayout(projects_row)
        self.project_list = QListWidget()
        self.project_list.setMaximumHeight(180)
        self.project_list.currentItemChanged.connect(self._project_selected)
        layout.addWidget(self.project_list)
        project_actions = QHBoxLayout()
        remove_project = QPushButton("Remove")
        remove_project.setObjectName("ghost")
        remove_project.clicked.connect(self._remove_project)
        project_actions.addWidget(remove_project)
        recycle_project = QPushButton("Recycle folder")
        recycle_project.setObjectName("danger")
        recycle_project.clicked.connect(self._recycle_project)
        project_actions.addWidget(recycle_project)
        layout.addLayout(project_actions)

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
        knowledge_button = QPushButton("Project knowledge")
        knowledge_button.setObjectName("ghost")
        knowledge_button.clicked.connect(self._open_knowledge_review)
        model_row.addWidget(knowledge_button)
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

        self.pending_build_card = QFrame()
        self.pending_build_card.setObjectName("metricCard")
        pending_layout = QHBoxLayout(self.pending_build_card)
        self.pending_build_label = QLabel()
        self.pending_build_label.setWordWrap(True)
        pending_layout.addWidget(self.pending_build_label, 1)
        self.pending_build_review = QPushButton("Review in Build")
        self.pending_build_review.clicked.connect(self._review_pending_build)
        pending_layout.addWidget(self.pending_build_review)
        self.pending_build_cancel = QPushButton("Cancel")
        self.pending_build_cancel.clicked.connect(self._cancel_pending_build)
        pending_layout.addWidget(self.pending_build_cancel)
        self.pending_build_card.setVisible(False)
        layout.addWidget(self.pending_build_card)

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
        self.build_request.textChanged.connect(self._refresh_build_readiness)
        self.build_request.send_requested.connect(self._run_build)
        request_layout.addWidget(self.build_request)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Provider"))
        self.build_provider = QComboBox()
        self.build_provider.addItem("Codex", "codex")
        self.build_provider.addItem("Claude", "claude")
        self.build_provider.addItem("Copilot", "copilot")
        self.build_provider.currentIndexChanged.connect(self._build_preferences_changed)
        controls.addWidget(self.build_provider)

        controls.addWidget(QLabel("Task owner"))
        self.build_owner = QComboBox()
        self.build_owner.addItem("Internal Build", "internal")
        self.build_owner.addItem("Kandev", "kandev")
        self.build_owner.setToolTip("Choose who owns new tasks: MoliCoder Internal Build or Kandev.")
        self.build_owner.currentIndexChanged.connect(self._refresh_build_readiness)
        controls.addWidget(self.build_owner)
        self.kandev_board_button = QPushButton("Open Kandev board")
        self.kandev_board_button.setToolTip("Open Kandev's own task board in your browser.")
        self.kandev_board_button.clicked.connect(self._open_kandev_board)
        controls.addWidget(self.kandev_board_button)
        controls.addWidget(QLabel("Fallback on provider failure"))
        self.build_fallback_provider = QComboBox()
        self.build_fallback_provider.addItem("None", "")
        self.build_fallback_provider.addItem("Codex", "codex")
        self.build_fallback_provider.addItem("Claude", "claude")
        self.build_fallback_provider.addItem("Copilot", "copilot")
        self.build_fallback_provider.setToolTip(
            "Optional for new Build runs. Uses one remaining task attempt only after a provider failure."
        )
        controls.addWidget(self.build_fallback_provider)
        controls.addWidget(QLabel("Strategy"))
        self.build_strategy = QComboBox()
        self.build_strategy.addItem("Hybrid", "HYBRID")
        self.build_strategy.addItem("Frontier only", "FRONTIER_ONLY")
        self.build_strategy.addItem("Local offline", "LOCAL_OFFLINE")
        self.build_strategy.currentIndexChanged.connect(self._build_preferences_changed)
        self.build_mode = self.build_strategy
        controls.addWidget(self.build_strategy)
        provider_settings = QPushButton("Provider settings")
        provider_settings.clicked.connect(self._provider_settings)
        controls.addWidget(provider_settings)
        self.doctor_button = QPushButton("Environment doctor")
        self.doctor_button.clicked.connect(self._show_environment_doctor)
        controls.addWidget(self.doctor_button)
        usage_button = QPushButton("Usage and budgets")
        usage_button.clicked.connect(self._show_usage_budgets)
        controls.addWidget(usage_button)
        evidence_button = QPushButton("Task evidence")
        evidence_button.clicked.connect(self._show_task_evidence)
        controls.addWidget(evidence_button)
        controls.addStretch()
        self.build_run_button = QPushButton("Run build")
        self.build_run_button.setObjectName("primary")
        self.build_run_button.setEnabled(False)
        self.build_run_button.clicked.connect(self._run_build)
        controls.addWidget(self.build_run_button)
        self.build_pause_button = QPushButton("Pause after step")
        self.build_pause_button.setEnabled(False)
        self.build_pause_button.clicked.connect(self._toggle_build_pause)
        controls.addWidget(self.build_pause_button)
        self.build_cancel_button = QPushButton("Cancel")
        self.build_cancel_button.setObjectName("danger")
        self.build_cancel_button.setEnabled(False)
        self.build_cancel_button.clicked.connect(self._cancel_build)
        controls.addWidget(self.build_cancel_button)
        request_layout.addLayout(controls)
        self.build_agent_info = QLabel()
        self.build_agent_info.setObjectName("muted")
        self.build_agent_info.setWordWrap(True)
        request_layout.addWidget(self.build_agent_info)
        layout.addWidget(request_card)

        status_card = QFrame()
        status_card.setObjectName("metricCard")
        status_layout = QVBoxLayout(status_card)
        status_layout.addWidget(QLabel("RUN STATUS"))
        self.build_status = QLabel("Select a project and enter a request.")
        self.build_status.setObjectName("muted")
        self.build_status.setWordWrap(True)
        status_layout.addWidget(self.build_status)
        self.build_ownership_status = QLabel("")
        self.build_ownership_status.setObjectName("muted")
        self.build_ownership_status.setWordWrap(True)
        status_layout.addWidget(self.build_ownership_status)
        self.build_activity = QLabel("Idle")
        self.build_activity.setStyleSheet("color: #22D3EE; font-weight: 650;")
        status_layout.addWidget(self.build_activity)
        self.build_events = QListWidget()
        self.build_events.setMinimumHeight(180)
        self.build_events.currentItemChanged.connect(self._build_event_selected)
        status_layout.addWidget(self.build_events)
        self.build_details = QTextBrowser()
        self.build_details.setPlaceholderText("Integrated diff and frontier review details appear here.")
        self.build_details.setMinimumHeight(150)
        status_layout.addWidget(self.build_details)
        self.build_log_button = QPushButton("Open run log")
        self.build_log_button.setEnabled(False)
        self.build_log_button.clicked.connect(self._open_build_log)
        status_layout.addWidget(self.build_log_button)
        recovery_row = QHBoxLayout()
        self.build_resume_button = QPushButton("Resume preserved run")
        self.build_resume_button.setEnabled(False)
        self.build_resume_button.clicked.connect(self._resume_preserved_run)
        recovery_row.addWidget(self.build_resume_button)
        self.build_open_worktree_button = QPushButton("Open worktree")
        self.build_open_worktree_button.setEnabled(False)
        self.build_open_worktree_button.clicked.connect(self._open_preserved_worktree)
        recovery_row.addWidget(self.build_open_worktree_button)
        self.build_discard_button = QPushButton("Discard worktree")
        self.build_discard_button.setObjectName("danger")
        self.build_discard_button.setEnabled(False)
        self.build_discard_button.clicked.connect(self._discard_preserved_run)
        recovery_row.addWidget(self.build_discard_button)
        status_layout.addLayout(recovery_row)
        self.build_merge_button = QPushButton("Apply ready result")
        self.build_merge_button.setEnabled(False)
        self.build_merge_button.clicked.connect(self._merge_last_build)
        status_layout.addWidget(self.build_merge_button)
        layout.addWidget(status_card, 1)
        return center

    def _show_environment_doctor(self) -> None:
        if self.doctor_dialog is not None:
            self.doctor_dialog.show()
            self.doctor_dialog.raise_()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Environment readiness")
        dialog.resize(700, 460)
        layout = QVBoxLayout(dialog)
        progress = QLabel("Checking installed tools and hardware…")
        layout.addWidget(progress)
        report_view = QTextBrowser(dialog)
        layout.addWidget(report_view)
        refresh = QPushButton("Refresh checks")
        layout.addWidget(refresh)
        managed_install = QPushButton("Install supported managed tool")
        managed_install.setEnabled(False)
        layout.addWidget(managed_install)
        managed_repair = QPushButton("Repair receipt-backed managed tool")
        managed_repair.setEnabled(False)
        layout.addWidget(managed_repair)
        install_ollama = QPushButton("Install verified Ollama runtime (Windows)")
        install_ollama.setEnabled(False)
        layout.addWidget(install_ollama)
        pull_model = QPushButton("Install Ollama library model")
        layout.addWidget(pull_model)
        self.doctor_dialog = dialog
        latest_report = None

        def show_report(report) -> None:
            nonlocal latest_report
            from ..environment.doctor import render_human
            from ..environment.manifests import load_manifests
            from ..environment.maintenance import plan_maintenance
            from ..environment.readiness import readiness_dashboard

            latest_report = report
            installable = tuple(
                manifest for manifest in load_manifests()
                if manifest.system == report.environment.system
                and manifest.architecture == report.environment.architecture
                and manifest.install_kind == "archive"
                and manifest.source_url.endswith((".tar.gz", ".tgz", ".tar.zst", ".zip"))
            )
            managed_install.setEnabled(bool(installable) and self.managed_install_worker is None)
            install_ollama.setEnabled(
                any(item.tool_id == "ollama" and item.system == "Windows"
                    and item.architecture == report.environment.architecture
                    and item.install_kind == "installer" for item in load_manifests())
                and self.ollama_install_worker is None
            )
            repairable = False
            for manifest in installable:
                try:
                    plan = plan_maintenance(
                        self.store, data_dir() / "tools", "repair", manifest.tool_id, manifest.version,
                    )
                    repairable = repairable or plan.sha256 == manifest.sha256
                except (OSError, ValueError, KeyError):
                    continue
            managed_repair.setEnabled(repairable and self.managed_repair_worker is None)
            dashboard = readiness_dashboard(report)
            summary = (
                f"Chat shell ready · local generation: {'ready' if dashboard.local_chat_ready else 'limited'} · "
                f"local Build: {'ready' if dashboard.local_build_ready else 'limited'}\n\n"
            )
            report_view.setPlainText(summary + render_human(report))
            counts = {state: sum(item.state == state for item in dashboard.components) for state in (
                "ready", "limited", "login_needed", "permission_needed", "failed", "unsupported", "update_available",
            )}
            progress.setText(
                "Installed/ready: {ready} · Ready with limits: {limited} · Waiting login: {login_needed} · "
                "Waiting permission: {permission_needed} · Failed: {failed} · Unsupported: {unsupported} · "
                "Update available: {update_available}".format(**counts)
            )

        def check_again() -> None:
            if self.doctor_worker is not None:
                return
            progress.setText("Checking installed tools and hardware…")
            refresh.setEnabled(False)
            worker = DoctorWorker()
            self.doctor_worker = worker
            worker.ready.connect(show_report)
            worker.failed.connect(lambda error: (
                progress.setText("Doctor failed"),
                report_view.setPlainText(f"Read-only doctor failed: {error}"),
            ))
            worker.finished.connect(lambda: (
                setattr(self, "doctor_worker", None), refresh.setEnabled(True),
            ))
            worker.start()

        refresh.clicked.connect(check_again)
        def begin_managed_install() -> None:
            from ..environment.manifests import load_manifests
            from ..environment.policy import plan_setup

            if latest_report is None or self.managed_install_worker is not None:
                return
            candidates = tuple(
                manifest for manifest in load_manifests()
                if manifest.system == latest_report.environment.system
                and manifest.architecture == latest_report.environment.architecture
                and manifest.install_kind == "archive"
                and manifest.source_url.endswith((".tar.gz", ".tgz", ".tar.zst", ".zip"))
            )
            if not candidates:
                return
            choices = [f"{item.tool_id} {item.version}" for item in candidates]
            selected, accepted = QInputDialog.getItem(
                dialog, "Managed setup", "Supported tool", choices, 0, False,
            )
            if not accepted:
                return
            manifest = candidates[choices.index(selected)]
            try:
                plan = plan_setup(self.store, latest_report.environment, manifest)
            except ValueError as exc:
                QMessageBox.warning(dialog, "Managed setup", str(exc))
                return
            if plan.action != "install_managed":
                QMessageBox.information(
                    dialog, "Managed setup",
                    "An existing installation was found. Review its ownership before changing it.",
                )
                return
            answer = QMessageBox.question(
                dialog, "Approve managed install",
                f"Install {manifest.tool_id} {manifest.version} for this user?\n\n"
                f"Download: {plan.download_bytes / 1_000_000:.1f} MB from {manifest.source_url}\n"
                f"SHA-256: {manifest.sha256}\n"
                f"Destination: {data_dir() / 'tools'}\n\n"
                "The archive will be verified before extraction. Its executable will be checked after install.\n"
                f"Rollback: {manifest.rollback}",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.store.record_action_authorization(
                project_id=None, action="setup", scope=plan.scope, risk="mutation",
                decision="approved", actor="user",
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                reason="approved in Environment readiness",
            )
            approved_plan = plan_setup(self.store, latest_report.environment, manifest)
            worker = ManagedInstallWorker(self.store, approved_plan, manifest, data_dir() / "tools")
            self.managed_install_worker = worker
            managed_install.setEnabled(False)
            progress.setText(f"Installing {manifest.tool_id} {manifest.version}…")
            worker.completed.connect(lambda receipt: (
                QMessageBox.information(dialog, "Managed setup", f"Verified installation: {receipt.executable}"),
                check_again(),
            ))
            worker.failed.connect(lambda error: (
                QMessageBox.warning(dialog, "Managed setup failed", error),
                progress.setText("Managed install failed; review the receipt or retry."),
            ))
            worker.finished.connect(lambda: (
                setattr(self, "managed_install_worker", None),
                managed_install.setEnabled(True),
                worker.deleteLater(),
            ))
            worker.start()

        managed_install.clicked.connect(begin_managed_install)
        def begin_managed_repair() -> None:
            from ..environment.manifests import load_manifests
            from ..environment.maintenance import plan_maintenance, repair_setup_plan

            if latest_report is None or self.managed_repair_worker is not None:
                return
            candidates = []
            for manifest in load_manifests():
                if (manifest.system != latest_report.environment.system
                        or manifest.architecture != latest_report.environment.architecture
                        or manifest.install_kind != "archive"
                        or not manifest.source_url.endswith((".tar.gz", ".tgz", ".tar.zst", ".zip"))):
                    continue
                try:
                    plan = plan_maintenance(
                        self.store, data_dir() / "tools", "repair", manifest.tool_id, manifest.version,
                    )
                except (OSError, ValueError, KeyError):
                    continue
                if plan.sha256 == manifest.sha256:
                    candidates.append((manifest, plan))
            if not candidates:
                return
            choices = [f"{manifest.tool_id} {manifest.version}" for manifest, _plan in candidates]
            selected, accepted = QInputDialog.getItem(
                dialog, "Managed repair", "Receipt-backed tool", choices, 0, False,
            )
            if not accepted:
                return
            manifest, plan = candidates[choices.index(selected)]
            setup_plan = repair_setup_plan(self.store, manifest)
            answer = QMessageBox.question(
                dialog, "Approve managed repair",
                f"Repair {manifest.tool_id} {manifest.version} under {data_dir() / 'tools'}?\n\n"
                f"Download: {manifest.size_bytes / 1_000_000:.1f} MB from {manifest.source_url}\n"
                f"SHA-256: {manifest.sha256}\n\n"
                "The current receipt-backed version will be kept as a repair backup. "
                "The replacement executable will be version checked.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            for action, scope, risk in (
                ("maintenance", plan.scope, plan.risk.value),
                ("setup", setup_plan.scope, "mutation"),
            ):
                self.store.record_action_authorization(
                    project_id=None, action=action, scope=scope, risk=risk,
                    decision="approved", actor="user", expires_at=expires,
                    reason="approved exact managed repair in Environment readiness",
                )
            worker = ManagedRepairWorker(
                self.store, plan, repair_setup_plan(self.store, manifest),
                manifest, data_dir() / "tools",
            )
            self.managed_repair_worker = worker
            managed_repair.setEnabled(False)
            progress.setText(f"Repairing {manifest.tool_id} {manifest.version}…")
            worker.completed.connect(lambda receipt: (
                QMessageBox.information(dialog, "Managed repair", f"Verified repaired tool: {receipt.executable}"),
                check_again(),
            ))
            worker.failed.connect(lambda error: (
                QMessageBox.warning(dialog, "Managed repair failed", error),
                progress.setText("Managed repair failed; original backup was retained or restored."),
            ))
            worker.finished.connect(lambda: (
                setattr(self, "managed_repair_worker", None),
                managed_repair.setEnabled(True),
                worker.deleteLater(),
            ))
            worker.start()

        managed_repair.clicked.connect(begin_managed_repair)
        def begin_ollama_install() -> None:
            from ..environment.manifests import load_manifests
            from ..environment.policy import plan_setup

            if latest_report is None or self.ollama_install_worker is not None:
                return
            manifest = next((item for item in load_manifests()
                             if item.tool_id == "ollama" and item.system == "Windows"
                             and item.architecture == latest_report.environment.architecture
                             and item.install_kind == "installer"), None)
            if manifest is None:
                return
            try:
                plan = plan_setup(self.store, latest_report.environment, manifest)
            except ValueError as exc:
                QMessageBox.warning(dialog, "Ollama setup", str(exc))
                return
            if plan.action != "install_managed":
                QMessageBox.information(dialog, "Ollama setup", "Ollama is already installed. Review the existing installation before changing it.")
                return
            answer = QMessageBox.question(
                dialog, "Approve Ollama installer",
                f"Install official Ollama {manifest.version} for this Windows user?\n\n"
                f"Download: {plan.download_bytes / 1_000_000_000:.2f} GB from {manifest.source_url}\n"
                f"SHA-256: {manifest.sha256}\n"
                f"Estimated free disk needed: {plan.estimated_disk_bytes / 1_000_000_000:.2f} GB\n"
                f"Destination: {data_dir() / 'tools' / f'ollama-{manifest.version}'}\n\n"
                "The app will check SHA-256 and Windows Authenticode, then run Ollama's official "
                "user-scope installer silently and verify its version. Ollama manages its separate model store.\n"
                f"Rollback: {manifest.rollback}",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.store.record_action_authorization(
                project_id=None, action="setup", scope=plan.scope, risk="mutation",
                decision="approved", actor="user",
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                reason="approved exact official Ollama installer in Environment readiness",
            )
            worker = WindowsOllamaInstallWorker(
                self.store, plan_setup(self.store, latest_report.environment, manifest),
                manifest, data_dir() / "tools",
            )
            self.ollama_install_worker = worker
            install_ollama.setEnabled(False)
            progress.setText(f"Installing Ollama {manifest.version}…")
            worker.completed.connect(lambda receipt: (
                QMessageBox.information(dialog, "Ollama installed", f"Verified {receipt.executable}"),
                check_again(), self._start_ollama_check(),
            ))
            worker.failed.connect(lambda error: (
                QMessageBox.warning(dialog, "Ollama setup failed", error),
                progress.setText("Ollama setup failed; review the installer state before retrying."),
            ))
            worker.finished.connect(lambda: (
                setattr(self, "ollama_install_worker", None),
                install_ollama.setEnabled(True), worker.deleteLater(),
            ))
            worker.start()

        install_ollama.clicked.connect(begin_ollama_install)
        def begin_model_pull() -> None:
            if self.model_inspect_worker is not None or self.model_pull_worker is not None:
                return
            model, accepted = QInputDialog.getText(
                dialog, "Install Ollama model", "Official Ollama library model and tag (for example qwen2.5-coder:7b):",
            )
            if not accepted or not model.strip():
                return
            model = model.strip()
            pull_model.setEnabled(False)
            progress.setText(f"Checking official manifest for {model}…")
            worker = ModelInspectWorker(model)
            self.model_inspect_worker = worker

            def approve_model(plan) -> None:
                if not plan.suitable:
                    QMessageBox.warning(dialog, "Model suitability", plan.reason)
                    progress.setText("Model download was not started.")
                    return
                answer = QMessageBox.question(
                    dialog, "Approve model download",
                    f"Pull {plan.model} into your Ollama model store?\n\n"
                    f"Official manifest: {plan.source}\n"
                    f"Manifest layer size: {plan.size_bytes / 1_000_000_000:.2f} GB\n"
                    f"Conservative RAM: {plan.ram_needed_bytes / 1_000_000_000:.2f} GB\n"
                    f"Preferred VRAM: {plan.vram_preferred_bytes / 1_000_000_000:.2f} GB\n"
                    f"Conservative free disk: {plan.disk_needed_bytes / 1_000_000_000:.2f} GB\n\n"
                    "Ollama downloads the model after approval. Its model store is managed by Ollama, not MoliCoder.",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    progress.setText("Model download was not approved.")
                    return
                self.store.record_action_authorization(
                    project_id=None, action="pull_local_model", scope=plan.scope,
                    risk="mutation", decision="approved", actor="user",
                    expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    reason="approved exact model and manifest in Environment readiness",
                )
                pull_worker = ModelPullWorker(self.store, self.client, plan)
                self.model_pull_worker = pull_worker
                progress.setText(f"Downloading {plan.model} through Ollama…")
                pull_worker.completed.connect(lambda name: (
                    QMessageBox.information(dialog, "Model installed", f"{name} is ready in Ollama."),
                    self._start_ollama_check(), check_again(),
                ))
                pull_worker.failed.connect(lambda error: (
                    QMessageBox.warning(dialog, "Model download failed", error),
                    progress.setText("Model download failed; Ollama may retain partial layers."),
                ))
                pull_worker.finished.connect(lambda: (
                    setattr(self, "model_pull_worker", None), pull_model.setEnabled(True),
                    pull_worker.deleteLater(),
                ))
                pull_worker.start()

            worker.ready.connect(approve_model)
            worker.failed.connect(lambda error: (
                QMessageBox.warning(dialog, "Model manifest check failed", error),
                progress.setText("Model manifest could not be verified."),
            ))
            worker.finished.connect(lambda: (
                setattr(self, "model_inspect_worker", None),
                pull_model.setEnabled(self.model_pull_worker is None), worker.deleteLater(),
            ))
            worker.start()

        pull_model.clicked.connect(begin_model_pull)
        dialog.finished.connect(lambda: setattr(self, "doctor_dialog", None))
        check_again()
        dialog.show()

    def _show_usage_budgets(self) -> None:
        project = self.registry.selected()
        if project is None:
            QMessageBox.information(self, "Usage and budgets", "Select a project first.")
            return
        from ..inference.usage_view import render_usage_report
        from ..inference.budget import BudgetManager

        dialog = QDialog(self)
        dialog.setWindowTitle("Usage and budgets")
        dialog.resize(720, 480)
        layout = QVBoxLayout(dialog)
        report = QTextBrowser(dialog)
        layout.addWidget(report)
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("Project API token-charge limit (USD, all time)"))
        project_limit = QDoubleSpinBox(dialog)
        project_limit.setDecimals(4)
        project_limit.setRange(0, 10000)
        project_limit.setSingleStep(1)
        current_limit = BudgetManager(self.store).project_limit(project.id)
        project_limit.setValue(current_limit or 0)
        limit_row.addWidget(project_limit)
        save_limit = QPushButton("Save hard limit")
        limit_row.addWidget(save_limit)
        layout.addLayout(limit_row)
        refresh = QPushButton("Refresh usage")
        refresh.clicked.connect(lambda: report.setPlainText(
            render_usage_report(self.store, self.inference_registry, project.id)
        ))
        layout.addWidget(refresh)
        def save_project_limit() -> None:
            BudgetManager(self.store).set_project_limit(
                project.id, project_limit.value(), actor="user",
            )
            report.setPlainText(render_usage_report(self.store, self.inference_registry, project.id))
            QMessageBox.information(
                dialog, "Project budget",
                f"Project API spending limit set to ${project_limit.value():.4f}. "
                "Each paid request still needs exact-route approval and a request-bound reservation.",
            )

        save_limit.clicked.connect(save_project_limit)
        refresh.click()
        dialog.show()

    def _show_task_evidence(self) -> None:
        project = self.registry.selected()
        if project is None:
            QMessageBox.information(self, "Task evidence", "Select a project first.")
            return
        runs = self.store.list_project_runs(project.id)
        if not runs:
            QMessageBox.information(self, "Task evidence", "No Build runs have been recorded for this project.")
            return
        from ..molicoder.evidence_view import render_task_evidence

        dialog = QDialog(self)
        dialog.setWindowTitle("Task evidence")
        dialog.resize(720, 480)
        layout = QVBoxLayout(dialog)
        run_choice = QComboBox(dialog)
        for run in runs:
            run_choice.addItem(f"Run {run['id']} · {run['status']}", run["id"])
        layout.addWidget(run_choice)
        report = QTextBrowser(dialog)
        layout.addWidget(report)
        export_trace = QPushButton("Export privacy-safe trace")

        def export_selected_trace() -> None:
            from ..observability import TraceExporter

            try:
                path = TraceExporter(self.store).export_run(int(run_choice.currentData()))
            except (OSError, ValueError, PermissionError) as exc:
                QMessageBox.warning(dialog, "Trace export failed", str(exc))
                return
            QMessageBox.information(dialog, "Trace exported", str(path))

        export_trace.clicked.connect(export_selected_trace)
        layout.addWidget(export_trace)
        run_choice.currentIndexChanged.connect(lambda: report.setPlainText(
            render_task_evidence(self.store, int(run_choice.currentData()))
        ))
        report.setPlainText(render_task_evidence(self.store, int(run_choice.currentData())))
        dialog.show()

    def _open_knowledge_review(self) -> None:
        if self.current_project_id is None:
            QMessageBox.information(self, "Project knowledge", "Select a project first.")
            return
        dialog = KnowledgeReviewDialog(self.store, self.current_project_id, self)
        dialog.exec()

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
        self.pacing_label = QLabel("Paid API pacing: select a project.")
        self.pacing_label.setObjectName("muted")
        self.pacing_label.setWordWrap(True)
        layout.addWidget(self.pacing_label)

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
        project_menu = self.menuBar().addMenu("Project")
        provider_action = QAction("Provider settings", self)
        provider_action.triggered.connect(self._provider_settings)
        project_menu.addAction(provider_action)
        clear_history = QAction("Clear build history", self)
        clear_history.triggered.connect(self._clear_project_history)
        project_menu.addAction(clear_history)
        clean_worktrees = QAction("Clean failed run worktrees", self)
        clean_worktrees.triggered.connect(self._clean_failed_worktrees)
        project_menu.addAction(clean_worktrees)

    def _start_ollama_check(self) -> None:
        self.startup_worker = StartupWorker(self.client)
        self.startup_worker.ready.connect(self._ollama_ready)
        self.startup_worker.finished.connect(
            lambda: self._startup_worker_finished(self.startup_worker)
        )
        self.startup_worker.start()

    def _ollama_ready(self, available: bool, models: list[dict]) -> None:
        self._installed_model_names = {str(model.get("name", "")) for model in models}
        self._installed_model_presets = {
            name: replace(
                MODEL_PRESETS["molicoder:latest"], name=name, label=name,
                role="Installed Ollama model", context_window=8192,
                max_output_tokens=2048, show_thinking=False,
            )
            for name in self._installed_model_names if name and name not in MODEL_PRESETS
        }
        for preset in self._installed_model_presets.values():
            ensure_local_offering(
                self.inference_registry, preset.name,
                context_window=preset.context_window,
                max_output_tokens=preset.max_output_tokens,
            )
        selected = self.model_combo.currentData()
        if self.current_conversation_id is not None:
            conversation = self.store.get_conversation(self.current_conversation_id)
            if conversation and conversation.model in self._installed_model_presets:
                selected = conversation.model
        self.model_combo.blockSignals(True)
        for index in range(self.model_combo.count() - 1, -1, -1):
            if self.model_combo.itemData(index) not in MODEL_PRESETS:
                self.model_combo.removeItem(index)
        for name in sorted(self._installed_model_presets):
            self.model_combo.addItem(f"{name}  ·  Installed Ollama model", name)
        index = self.model_combo.findData(selected)
        self.model_combo.setCurrentIndex(index if index >= 0 else 0)
        self.model_combo.blockSignals(False)
        self._apply_preset_controls()
        for preset in (*MODEL_PRESETS.values(), *self._installed_model_presets.values()):
            present = available and preset.name in self._installed_model_names
            self.inference_registry.set_offering_state(
                local_offering_id(preset.name),
                status="eligible" if present else "unavailable",
                health_state="healthy" if present else "unavailable",
                actor="local-ollama-discovery", reason="model found in local Ollama tags" if present else "local model unavailable",
            )
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
        self._refresh_build_readiness()

    def _startup_worker_finished(self, worker: StartupWorker | None) -> None:
        if worker is None:
            return
        worker.deleteLater()
        if self.startup_worker is worker:
            self.startup_worker = None

    def _update_model_availability(self) -> None:
        model = self.preset.name
        missing = bool(self._installed_model_names) and model not in self._installed_model_names
        self._refresh_context_meter()
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
        self._refresh_pacing()
        if self.pending_build_request is not None:
            self._update_pending_build_card()

    def _update_build_project(self, project: Project | None) -> None:
        if project is None:
            self.build_project_title.setText("No project selected")
            self.build_project_meta.setText("Add a local project from the sidebar to begin.")
            self.build_request.setEnabled(False)
            self._refresh_recovery_actions(None)
            self._refresh_build_readiness()
            return
        path = Path(project.path)
        self.build_project_title.setText(project.name)
        if not path.is_dir():
            self.build_project_meta.setText(f"{project.kind.title()} · Folder missing\n{project.path}")
            self.build_request.setEnabled(False)
            self._refresh_build_readiness()
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
        preferences = self.store.get_project_preferences(project.id)
        self.build_provider.blockSignals(True)
        self.build_strategy.blockSignals(True)
        provider_index = self.build_provider.findData(preferences.provider)
        strategy_index = self.build_strategy.findData(preferences.strategy)
        self.build_provider.setCurrentIndex(max(0, provider_index))
        self.build_strategy.setCurrentIndex(max(0, strategy_index))
        self.build_provider.blockSignals(False)
        self.build_strategy.blockSignals(False)
        self.build_request.setEnabled(True)
        self._refresh_recovery_actions(project)
        self._refresh_build_readiness()

    def _refresh_recovery_actions(self, project: Project | None = None) -> None:
        project = project or self.registry.selected()
        run = self._recoverable_build_run(project)
        path = Path(str(run.get("worktree", ""))).resolve() if run and run.get("worktree") else None
        kandev_owned = bool(run and run.get("provider") == "kandev")
        available = bool(kandev_owned or (path and path.is_dir()))
        self.recoverable_run_id = int(run["id"]) if run and available else None
        self.build_resume_button.setEnabled(available and self.orchestration_worker is None)
        self.build_resume_button.setText("Reconcile Kandev run" if kandev_owned else "Resume preserved run")
        self.build_open_worktree_button.setEnabled(bool(path and path.is_dir()))
        self.build_discard_button.setEnabled(available and not kandev_owned and self.orchestration_worker is None)
        self.build_ownership_status.setText(
            f"Kandev owns run {run['id']} · {run['status']} · reconcile by saved external ID before further action."
            if kandev_owned else ""
        )
        if kandev_owned and run["status"] == "ready_to_apply" and path and path.is_dir():
            tasks = self.store.list_run_tasks(int(run["id"]))
            if len(tasks) == 1 and tasks[0].status == "completed" and tasks[0].branch:
                self.last_run_id = int(run["id"])
                self.last_integration_branch = tasks[0].branch
                self.last_integration_path = path
                self.build_merge_button.setText("Apply accepted Kandev result")
                self.build_merge_button.setEnabled(self.orchestration_worker is None)
        elif self.last_run_id is not None:
            last = self.store.get_run(self.last_run_id)
            if last and last["provider"] == "kandev":
                self.build_merge_button.setEnabled(False)
                self.build_merge_button.setText("Apply ready result")

    def _recoverable_build_run(self, project: Project | None) -> dict | None:
        if project is None:
            return None
        for run in self.store.list_project_runs(project.id):
            if run["provider"] == "kandev" and run["status"] in {
                "running", "needs_attention", "ready_to_apply", "cancelled", "failed",
            }:
                return run
            if (run["provider"] != "kandev" and run["status"] in {
                "needs_attention", "ready_to_apply", "cancelled", "failed",
            } and run.get("worktree") and Path(str(run["worktree"])).is_dir()):
                return run
        return None

    def _open_preserved_worktree(self) -> None:
        project = self.registry.selected()
        run = self._recoverable_build_run(project)
        if run and run.get("worktree") and Path(str(run["worktree"])).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(run["worktree"])))

    def _resume_preserved_run(self) -> None:
        project = self.registry.selected()
        run = self._recoverable_build_run(project)
        if project is None or run is None or self.orchestration_worker is not None:
            return
        if run["provider"] == "kandev":
            self._reconcile_kandev_run(project, run)
            return
        provider_name = str(self.build_provider.currentData() or run.get("provider") or "codex")
        preferences = self.store.get_project_preferences(project.id)
        if provider_name not in preferences.trusted_providers:
            QMessageBox.information(self, "Provider permission required", "Trust the selected provider in Provider settings before resuming.")
            return
        model = preferences.model_overrides.get(provider_name, "")
        mode = str(run.get("mode") or "HYBRID")
        request = str(run.get("request") or "")
        scope = (
            f"project:{project.id}:provider:{provider_name}:mode:{mode}:"
            f"resume:{run['id']}:request:{hashlib.sha256(request.encode('utf-8')).hexdigest()}"
        )
        self.store.record_action_authorization(
            project_id=project.id, action="build_run", scope=scope,
            risk=(ActionRisk.MUTATION if mode == "LOCAL_OFFLINE" else ActionRisk.EXTERNAL).value,
            decision="approved", actor="local_user_resume_button",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        )
        try:
            orchestrator = build_orchestrator(
                store=self.store, project=project, client=self.client,
                mode=mode, provider_name=provider_name, model=model,
                authorization_scope=scope,
            )
        except (OSError, ValueError, ProviderUnavailable) as exc:
            QMessageBox.critical(self, "Resume configuration error", str(exc))
            return
        worker = OrchestrationWorker(
            orchestrator, str(run.get("request") or ""), str(run.get("mode") or "HYBRID"),
            resume_run_id=int(run["id"]),
        )
        orchestrator.on_event = worker.relay_event
        worker.event.connect(self._build_event)
        worker.completed.connect(self._build_completed)
        worker.failed.connect(self._build_failed)
        worker.finished.connect(lambda: self._orchestration_worker_finished(worker))
        self.orchestration_worker = worker
        self.build_request.setEnabled(False)
        self.build_owner.setEnabled(False)
        self.build_mode.setEnabled(False)
        self.build_provider.setEnabled(False)
        self.build_fallback_provider.setEnabled(False)
        self.build_pause_button.setEnabled(True)
        self.build_cancel_button.setEnabled(True)
        self._build_started_at = time.monotonic()
        self._build_activity_timer.start()
        self._refresh_recovery_actions(project)
        worker.start()

    def _reconcile_kandev_run(self, project: Project, run: dict) -> None:
        try:
            adapter = configured_kandev_adapter(self.store)
            orchestrator = build_orchestrator(
                store=self.store, project=project, client=self.client,
                kandev_connection=adapter.connection, kandev_adapter=adapter,
            )
        except (OSError, ValueError, ProviderUnavailable) as exc:
            QMessageBox.warning(self, "Kandev recovery", str(exc))
            return
        worker = OrchestrationWorker(
            orchestrator, str(run["request"]), str(run["mode"]), resume_run_id=int(run["id"]),
        )
        orchestrator.on_event = worker.relay_event
        worker.event.connect(self._build_event)
        worker.completed.connect(self._build_completed)
        worker.failed.connect(self._build_failed)
        worker.finished.connect(lambda: self._orchestration_worker_finished(worker))
        self.orchestration_worker = worker
        self.build_request.setEnabled(False)
        self.build_owner.setEnabled(False)
        self.build_mode.setEnabled(False)
        self.build_provider.setEnabled(False)
        self.build_fallback_provider.setEnabled(False)
        self.build_pause_button.setEnabled(False)
        self.build_cancel_button.setEnabled(False)
        self._build_started_at = time.monotonic()
        self._build_activity_timer.start()
        self._refresh_recovery_actions(project)
        worker.start()

    def _discard_preserved_run(self) -> None:
        project = self.registry.selected()
        run = self._recoverable_build_run(project)
        if project is None or run is None or self.orchestration_worker is not None:
            return
        if run["provider"] == "kandev":
            QMessageBox.warning(self, "Kandev-owned run", "Reconcile the Kandev task before changing its worktree.")
            return
        path = Path(str(run.get("worktree") or "")).resolve()
        managed_root = (data_dir() / "worktrees").resolve()
        try:
            path.relative_to(managed_root)
        except ValueError:
            QMessageBox.critical(self, "Unsafe worktree path", f"Refusing to remove an unmanaged path:\n{path}")
            return
        if QMessageBox.question(
            self, "Discard preserved worktree",
            f"Permanently remove the isolated worktree for run {run['id']}?\n{path}\nThe real project checkout is not changed.",
        ) != QMessageBox.StandardButton.Yes:
            return
        branch = f"molicoder/run-{run['id']}"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["git", "-C", project.path, "worktree", "remove", "--force", str(path)],
            capture_output=True, check=False, creationflags=creationflags,
        )
        if path.exists():
            QDir(str(path)).removeRecursively()
        subprocess.run(
            ["git", "-C", project.path, "branch", "-D", branch],
            capture_output=True, check=False, creationflags=creationflags,
        )
        self.store.update_run_state(int(run["id"]), "failed", worktree="", recovery_reason="discarded by user")
        self.store.finish_run(int(run["id"]), "failed")
        self._refresh_recovery_actions(project)

    def _build_preferences_changed(self) -> None:
        project = self.registry.selected()
        if project is None:
            return
        existing = self.store.get_project_preferences(project.id)
        self.store.set_project_preferences(
            project.id,
            provider=str(self.build_provider.currentData() or "codex"),
            strategy=str(self.build_strategy.currentData() or "HYBRID"),
            trusted_providers=existing.trusted_providers,
            model_overrides=existing.model_overrides,
        )
        self._refresh_build_readiness()

    def _provider_settings(self) -> None:
        project = self.registry.selected()
        if project is None:
            QMessageBox.information(self, "Provider settings", "Select a project first.")
            return
        statuses = {status.provider: status for status in probe_all_providers()}
        preferences = self.store.get_project_preferences(project.id)
        selected = str(self.build_provider.currentData() or preferences.provider)
        dialog = QDialog(self)
        dialog.setWindowTitle("Frontier provider settings")
        dialog.setMinimumWidth(560)
        layout = QVBoxLayout(dialog)
        intro = QLabel("Subscription CLIs are used only by Build. Chat stays local through Ollama.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        for profile in AGENT_PROFILES:
            status = statuses.get(profile.provider)
            if status is None:
                availability = "Not probed"
            elif not status.installed:
                availability = "Unavailable: CLI is not installed"
            elif status.auth_verified:
                availability = "Sign-in verified" if status.authenticated else "Sign-in required"
            else:
                availability = "Installed; sign-in and entitlement unverified"
            if not profile.selectable:
                availability = f"Build selection unavailable: {profile.limitation}"
            route = "Local gateway preview" if profile.route_class == "local_gateway_preview" else "Opaque subscription CLI"
            detail = status.detail if status is not None else ""
            label = QLabel(
                f"{'OpenCode' if profile.provider == 'opencode' else profile.provider.title()} — {route}\n"
                f"   {availability}\n"
                f"   {detail}\n"
                f"   Permissions: {profile.permission_model}; cancellation: {profile.cancellation}.\n"
                f"   Sandbox: {profile.sandbox}\n"
                f"   Capabilities: {', '.join(profile.capabilities)}.\n"
                f"   {profile.limitation}"
            )
            label.setStyleSheet(
                "color: #34D399;" if status and status.installed and status.auth_verified
                and status.authenticated and profile.selectable else "color: #F59E0B;"
            )
            label.setWordWrap(True)
            layout.addWidget(label)
        opencode_guide = QPushButton("Open official OpenCode provider guide")
        opencode_guide.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(agent_profile("opencode").auth_guide))
        )
        layout.addWidget(opencode_guide)
        form = QFormLayout()
        model_edit = QLineEdit(preferences.model_overrides.get(selected, ""))
        model_edit.setPlaceholderText("Leave blank to use the CLI default")
        form.addRow(f"{selected.title()} model override", model_edit)
        trusted = QCheckBox(f"Trust {selected.title()} for this project")
        trusted.setChecked(selected in preferences.trusted_providers)
        form.addRow("Project permission", trusted)
        layout.addLayout(form)
        setup = QPushButton(f"Open official {selected.title()} setup and sign-in guide")
        setup.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(agent_profile(selected).auth_guide)))
        layout.addWidget(setup)
        terms = QPushButton(f"Open {selected.title()} account terms")
        terms.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(agent_profile(selected).terms_url)))
        layout.addWidget(terms)
        api_providers = tuple(
            item for item in self.inference_registry.list_providers()
            if item[0] not in {"ollama", "codex", "claude", "copilot"}
        )
        security = platform_security()
        security_label = QLabel(security.reason)
        security_label.setWordWrap(True)
        layout.addWidget(security_label)
        add_custom = QPushButton("Add custom OpenAI-compatible provider")
        add_custom.clicked.connect(self._add_custom_provider)
        layout.addWidget(add_custom)
        add_preset = QPushButton("Add official API preset")
        add_preset.clicked.connect(self._add_api_preset)
        layout.addWidget(add_preset)
        layout.addWidget(QLabel("API account readiness"))
        accounts = self.inference_registry.list_account_statuses()
        if not accounts:
            layout.addWidget(QLabel("No API accounts configured. Add a provider to begin setup."))
        for account in accounts:
            layout.addWidget(QLabel(
                f"{account.provider_id} ({account.id}): {account.auth_status}; "
                f"encrypted key {'stored' if account.credential_present else 'not stored'}"
            ))
        if api_providers:
            key_button = QPushButton("Store API key for a registered provider")
            key_button.setEnabled(security.credential_backend is not None)
            if security.credential_backend is None:
                key_button.setToolTip("No verified encrypted provider-key backend is available on this OS.")
            key_button.clicked.connect(lambda: self._store_provider_key(api_providers))
            layout.addWidget(key_button)
        custom_offerings = tuple(
            offering for offering in self.inference_registry.list_offerings()
            if is_api_offering(offering.id)
        )
        if custom_offerings:
            check_button = QPushButton("Check custom provider connection")
            check_button.clicked.connect(lambda: self._check_custom_provider_connection(custom_offerings))
            layout.addWidget(check_button)
            facts_button = QPushButton("Configure custom provider facts")
            facts_button.clicked.connect(lambda: self._configure_custom_provider_facts(custom_offerings))
            layout.addWidget(facts_button)
            run_custom = QPushButton("Run capped custom API test")
            run_custom.clicked.connect(lambda: self._run_custom_api_test(custom_offerings))
            layout.addWidget(run_custom)
        offerings = self.inference_registry.list_offerings()
        offering_controls: list[tuple[str, QCheckBox, str]] = []
        layout.addWidget(QLabel("Registered inference offerings"))
        if not offerings:
            layout.addWidget(QLabel("No inference offerings are registered."))
        for offering in offerings:
            auth = (self.inference_registry.get_account_status(offering.account_id)
                    if offering.account_id else "not required" if offering.offering_class == "local" else "unknown")
            capabilities = ", ".join(
                f"{key}={value}" for key, value in sorted(offering.capabilities.items())
            ) or "unknown"
            quota = ", ".join(
                f"{key}={value}" for key, value in sorted(offering.quota.items())
            ) or "unknown"
            details = QLabel(
                f"{offering.provider_id} / {offering.model_id} ({offering.id}) — {offering.offering_class}\n"
                f"   Auth: {auth}; health: {offering.health_state}; status: {offering.status}\n"
                f"   Capabilities: {capabilities}; privacy: {offering.privacy_class}; quota: {quota}\n"
                f"   Price checked: {offering.price_checked_at or 'unknown'}; "
                f"terms checked: {offering.terms_checked_at or 'unknown'}\n"
                f"   Source: {offering.source_url or 'unknown'}"
            )
            details.setWordWrap(True)
            layout.addWidget(details)
            enabled = QCheckBox(f"Enable {offering.id}")
            if (offering.offering_class in {"metered", "trial"}
                    and not security.metered_api_allowed):
                enabled.setEnabled(False)
                enabled.setToolTip("Metered API routes need a supported OS credential backend.")
            enabled.setChecked(offering.status == "eligible")
            offering_controls.append((offering.id, enabled, offering.status))
            layout.addWidget(enabled)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for offering_id, enabled, previous_status in offering_controls:
            desired_status = "eligible" if enabled.isChecked() else "disabled"
            if desired_status != previous_status and (enabled.isChecked() or previous_status == "eligible"):
                self.inference_registry.set_offering_state(
                    offering_id, status=desired_status, actor="user", reason="provider settings",
                )
        models = dict(preferences.model_overrides)
        override = model_edit.text().strip()
        if override:
            models[selected] = override
        else:
            models.pop(selected, None)
        trusted_providers = set(preferences.trusted_providers)
        if trusted.isChecked():
            trusted_providers.add(selected)
        else:
            trusted_providers.discard(selected)
        self.store.set_project_preferences(
            project.id, provider=selected,
            strategy=str(self.build_strategy.currentData() or "HYBRID"),
            trusted_providers=tuple(sorted(trusted_providers)), model_overrides=models,
        )
        self._refresh_build_readiness()

    def _add_custom_provider(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Add custom API provider")
        form = QFormLayout(dialog)
        provider_id = QLineEdit(dialog)
        provider_id.setPlaceholderText("my-provider")
        name = QLineEdit(dialog)
        name.setPlaceholderText("Provider display name")
        endpoint = QLineEdit(dialog)
        endpoint.setPlaceholderText("https://provider.example/v1")
        model = QLineEdit(dialog)
        model.setPlaceholderText("provider-model-id")
        form.addRow("Provider ID", provider_id)
        form.addRow("Name", name)
        form.addRow("API base URL", endpoint)
        form.addRow("Model ID", model)
        note = QLabel(
            "Registration makes no network request. The route stays inactive until its key, "
            "capabilities, price, terms, and connection have been verified."
        )
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            offering_id = register_custom_route(self.store, CustomRoute(
                provider_id.text().strip(), name.text().strip(), endpoint.text().strip(),
                model.text().strip(),
            ))
        except ValueError as exc:
            QMessageBox.warning(self, "Custom provider", str(exc))
            return
        QMessageBox.information(
            self, "Provider registered",
            f"{offering_id} is registered and inactive. Open Provider settings again to store its key.",
        )

    def _add_api_preset(self) -> None:
        choices = [item.name for item in API_PRESETS]
        selected, accepted = QInputDialog.getItem(
            self, "Add API preset", "Official provider", choices, 0, False,
        )
        if not accepted:
            return
        preset = API_PRESETS[choices.index(selected)]
        model_id, accepted = QInputDialog.getText(
            self, "Add API preset", "Model ID from the official model list",
        )
        if not accepted:
            return
        try:
            offering_id = register_preset_route(self.store, preset.id, model_id.strip())
        except ValueError as exc:
            QMessageBox.warning(self, "API preset", str(exc))
            return
        QMessageBox.information(
            self, "API preset registered",
            f"{offering_id} is inactive. Reopen Provider settings to store its key, check the model, "
            f"and review prices and terms.\n\nPricing: {preset.price_url}\nTerms: {preset.terms_url}",
        )

    def _store_provider_key(self, providers: tuple[tuple[str, str], ...]) -> None:
        choices = [f"{name} ({provider_id})" for provider_id, name in providers]
        selected, accepted = QInputDialog.getItem(
            self, "Store provider key", "Registered provider", choices, 0, False,
        )
        if not accepted:
            return
        provider_id = providers[choices.index(selected)][0]
        secret, accepted = QInputDialog.getText(
            self, "Store provider key", "API key", QLineEdit.EchoMode.Password,
        )
        if not accepted or not secret:
            return
        try:
            store_provider_key(
                self.inference_registry, ProtectedCredentialStore(data_dir() / "credentials"),
                provider_id, secret,
            )
        except (CredentialUnavailable, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Provider key", str(exc))
            return
        QMessageBox.information(
            self, "Provider key stored",
            "The key is encrypted for this Windows user. The provider remains inactive until an eligible route is configured.",
        )

    def _check_custom_provider_connection(self, offerings) -> None:
        choices = [f"{item.provider_id} / {item.model_id}" for item in offerings]
        selected, accepted = QInputDialog.getItem(
            self, "Check provider connection", "Registered model", choices, 0, False,
        )
        if not accepted:
            return
        offering = offerings[choices.index(selected)]
        try:
            credentials = (ProtectedCredentialStore(data_dir() / "credentials")
                           if offering.account_id else None)
            result = check_custom_connection(
                self.store, offering.id, credentials=credentials,
            )
        except (CredentialUnavailable, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Provider connection", str(exc))
            return
        QMessageBox.information(
            self, "Provider connection",
            f"{result.detail}\nThe route stays inactive until its price, terms, and capabilities are configured.",
        )

    def _configure_custom_provider_facts(self, offerings) -> None:
        choices = [f"{item.provider_id} / {item.model_id}" for item in offerings]
        selected, accepted = QInputDialog.getItem(
            self, "Custom provider facts", "Registered model", choices, 0, False,
        )
        if not accepted:
            return
        offering = offerings[choices.index(selected)]
        dialog = QDialog(self)
        dialog.setWindowTitle("Review custom provider facts")
        form = QFormLayout(dialog)
        source = QLineEdit(dialog)
        source.setPlaceholderText("https://provider.example/pricing")
        terms = QLineEdit(dialog)
        terms.setPlaceholderText("https://provider.example/terms")
        preset = next((item for item in API_PRESETS if item.id == offering.provider_id), None)
        if preset is not None:
            source.setText(preset.price_url)
            terms.setText(preset.terms_url)
        privacy = QComboBox(dialog)
        for value in ("public", "private", "restricted"):
            privacy.addItem(value.title(), value)
        context = QSpinBox(dialog)
        context.setRange(1, 10_000_000)
        context.setValue(8192)
        output = QSpinBox(dialog)
        output.setRange(1, 10_000_000)
        output.setValue(1024)
        input_price = QDoubleSpinBox(dialog)
        output_price = QDoubleSpinBox(dialog)
        for control in (input_price, output_price):
            control.setDecimals(4)
            control.setRange(0, 1_000_000)
            control.setSingleStep(0.1)
            control.setEnabled(offering.offering_class != "local")
        automation = QCheckBox("Official terms permit automated API use", dialog)
        supports_stream = QCheckBox("Provider documents chat streaming support", dialog)
        attested = QCheckBox("I reviewed the linked current prices, terms, privacy rules, and model limits", dialog)
        form.addRow("Official price/model source", source)
        form.addRow("Official terms", terms)
        form.addRow("Allowed data class", privacy)
        form.addRow("Context tokens", context)
        form.addRow("Maximum output tokens", output)
        form.addRow("Input USD / million tokens", input_price)
        form.addRow("Output USD / million tokens", output_price)
        form.addRow(automation)
        form.addRow(supports_stream)
        form.addRow(attested)
        note = QLabel("These are user-attested facts. The route stays inactive until its connection and exact project approval pass.")
        note.setWordWrap(True)
        form.addRow(note)
        if offering.provider_id == "openrouter-api":
            fee_note = QLabel(
                "OpenRouter may charge fees when credits are purchased. The project cap covers "
                "the token prices entered here; account fees and taxes are outside that cap."
            )
            fee_note.setWordWrap(True)
            form.addRow(fee_note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not attested.isChecked():
            QMessageBox.warning(self, "Custom provider facts", "Review and attest the linked facts before saving.")
            return
        try:
            attest_custom_route_facts(self.store, CustomRouteFacts(
                offering.id, source.text().strip(), terms.text().strip(),
                str(privacy.currentData()), context.value(), output.value(),
                None if offering.offering_class == "local" else input_price.value(),
                None if offering.offering_class == "local" else output_price.value(),
                automation.isChecked(), supports_stream.isChecked(),
            ))
        except ValueError as exc:
            QMessageBox.warning(self, "Custom provider facts", str(exc))
            return
        QMessageBox.information(
            self, "Custom provider facts",
            "Facts saved with user-attested provenance. Reopen Provider settings to review and enable this route.",
        )

    def _run_custom_api_test(self, offerings) -> None:
        project = self.registry.selected()
        if project is None or self.custom_api_worker is not None:
            return
        eligible = tuple(item for item in offerings if item.status == "eligible")
        if not eligible:
            QMessageBox.information(self, "Custom API test", "Enable a verified custom route first.")
            return
        choices = [f"{item.provider_id} / {item.model_id}" for item in eligible]
        selected, accepted = QInputDialog.getItem(
            self, "Custom API test", "Enabled model", choices, 0, False,
        )
        if not accepted:
            return
        offering = eligible[choices.index(selected)]
        prompt, accepted = QInputDialog.getMultiLineText(
            self, "Custom API test", "Short test prompt", "Reply with one short sentence.",
        )
        if not accepted:
            return
        try:
            preview = preview_custom_chat(self.inference_registry, offering.id, prompt)
        except ValueError as exc:
            QMessageBox.warning(self, "Custom API test", str(exc))
            return
        cost = (f"${preview.worst_case_usd:.6f} maximum token charges reserved"
                if preview.worst_case_usd is not None else "no API price")
        if offering.provider_id == "openrouter-api":
            cost += "; credit-purchase fees and taxes are outside this cap"
        answer = QMessageBox.question(
            self, "Approve exact API route",
            f"Send this prompt to {offering.provider_id} / {offering.model_id}?\n"
            f"Endpoint: {offering.route_id}\nProject: {project.name}\n"
            f"Data class: private\nInput cap: {preview.max_input_tokens} tokens; "
            f"output cap: {preview.max_output_tokens} tokens\n{cost}\n\n"
            "The project hard limit and provider policy will be checked again before dispatch.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if offering.offering_class != "local":
            self.store.record_action_authorization(
                project_id=project.id, action="inference_route", scope=offering.id,
                risk="financial" if offering.offering_class in {"metered", "trial"} else "external",
                decision="approved", actor="user",
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                reason="approved exact custom API test in Provider settings",
            )
        worker = CustomApiWorker(self.store, project.id, offering.id, prompt)
        self.custom_api_worker = worker
        progress = QDialog(self)
        progress.setWindowTitle("Custom API test")
        progress_layout = QVBoxLayout(progress)
        progress_layout.addWidget(QLabel("Running a capped test request…"))
        cancel = QPushButton("Cancel request")
        cancel.clicked.connect(worker.cancel)
        progress.rejected.connect(worker.cancel)
        progress_layout.addWidget(cancel)
        worker.completed.connect(lambda result: (
            progress.close(),
            QMessageBox.information(
                self, "Custom API response",
                f"{result.content}\n\nInput tokens: {result.input_tokens if result.input_tokens is not None else 'unknown'}; "
                f"output tokens: {result.output_tokens if result.output_tokens is not None else 'unknown'}.",
            ),
        ))
        worker.failed.connect(lambda error: (
            progress.close(), QMessageBox.warning(self, "Custom API test failed", error),
        ))
        worker.finished.connect(lambda: (
            progress.close(), setattr(self, "custom_api_worker", None), worker.deleteLater(),
        ))
        worker.start()
        progress.show()

    def _refresh_build_readiness(self) -> None:
        if not hasattr(self, "build_status"):
            return
        kandev_selected = self.build_owner.currentData() == "kandev"
        internal_controls = not kandev_selected and self.orchestration_worker is None
        self.build_provider.setEnabled(internal_controls)
        self.build_fallback_provider.setEnabled(internal_controls)
        self.build_strategy.setEnabled(internal_controls)
        if kandev_selected:
            self.build_agent_info.setText(
                "Kandev owns the task and agent session. MoliCoder keeps local acceptance and apply control. "
                "A pending task is reconciled by its saved external ID; it never switches to Internal Build."
            )
        else:
            profile = agent_profile(str(self.build_provider.currentData() or "codex"))
            route = "Strict local gateway preview" if profile.route_class == "local_gateway_preview" else "Opaque subscription CLI"
            self.build_agent_info.setText(
                f"{profile.provider.title()}: {route}. {profile.sandbox} "
                f"{profile.limitation} Sign-in and entitlement are checked before Build starts; "
                "open Provider settings for installation and sign-in status."
            )
        project = self.registry.selected()
        request = self.build_request.toPlainText().strip()
        ready = False
        if self.orchestration_worker is not None:
            status = "Build is running locally. Follow the event timeline below."
        elif request and requested_folder(request) is not None:
            ready = True
            status = "Ready to confirm a local folder action. No coding agent will run."
        elif project is None:
            status = "Select a project before running a build."
        elif not Path(project.path).is_dir():
            status = "The selected project folder is missing."
        else:
            git_state = read_git_state(project.path)
            if not git_state.available:
                status = "Build mode requires a Git repository."
            elif git_state.branch == "Detached HEAD":
                status = "Switch to a named Git branch before running Build mode."
            elif git_state.dirty:
                status = "Commit or stash current changes first; Build mode requires a clean working tree."
            elif not request:
                status = "Ready for a request on this protected project branch."
            elif kandev_selected:
                try:
                    configured_kandev_adapter(self.store)
                except ValueError as exc:
                    status = str(exc)
                else:
                    ready = True
                    status = "Kandev selected · local settings valid · endpoint and task capabilities checked on launch."
            elif self.build_strategy.currentData() == "LOCAL_OFFLINE" and self._installed_model_names and "molicoder:latest" not in self._installed_model_names:
                status = "The molicoder:latest model is not installed in Ollama."
            else:
                ready = True
                provider = str(self.build_provider.currentData() or "codex").title()
                strategy = str(self.build_strategy.currentText())
                status = f"Ready · {provider} · {strategy} · isolated sequential execution and automatic validation."
        self.build_status.setText(status)
        self.build_run_button.setEnabled(ready)

    def _run_build(self) -> None:
        if self.orchestration_worker is not None:
            return
        folder = requested_folder(self.build_request.toPlainText())
        if folder is not None:
            result = self._confirm_directory_creation(folder)
            self.build_status.setText(result.content)
            return
        self._refresh_build_readiness()
        if not self.build_run_button.isEnabled():
            return
        project = self.registry.selected()
        request = self.build_request.toPlainText().strip()
        if project is None or not request:
            return
        if self.build_owner.currentData() == "kandev":
            self._run_kandev_build(project, request)
            return
        mode = str(self.build_strategy.currentData() or "HYBRID")
        provider_name = str(self.build_provider.currentData() or "codex")
        fallback_name = str(self.build_fallback_provider.currentData() or "")
        preferences = self.store.get_project_preferences(project.id)
        if fallback_name:
            if mode == "LOCAL_OFFLINE" or fallback_name == provider_name:
                QMessageBox.information(self, "Fallback provider", "Choose a distinct fallback for a provider-backed Build.")
                return
            if fallback_name not in preferences.trusted_providers:
                QMessageBox.information(
                    self, "Fallback provider",
                    "Trust the fallback provider for this project in Provider settings before running Build.",
                )
                return
        if mode != "LOCAL_OFFLINE" and provider_name not in preferences.trusted_providers:
            if QMessageBox.question(
                self,
                f"Trust {provider_name.title()} for this project",
                (
                    f"Allow {provider_name.title()} to plan and edit only inside an isolated worktree for {project.name}? "
                    "Validation must pass before MoliCoder applies changes. This permission can be revoked in Provider settings."
                ),
            ) != QMessageBox.StandardButton.Yes:
                return
            trusted = tuple(sorted(set(preferences.trusted_providers) | {provider_name}))
            self.store.set_project_preferences(
                project.id, provider=provider_name, strategy=mode,
                trusted_providers=trusted, model_overrides=preferences.model_overrides,
            )
            preferences = self.store.get_project_preferences(project.id)
        model = preferences.model_overrides.get(provider_name, "")
        risk = ActionRisk.MUTATION if mode == "LOCAL_OFFLINE" else ActionRisk.EXTERNAL
        request_hash = hashlib.sha256(request.encode("utf-8")).hexdigest()
        scope = f"project:{project.id}:provider:{provider_name}:mode:{mode}:request:{request_hash}"
        fallback_scope = (
            f"project:{project.id}:provider:{fallback_name}:mode:{mode}:request:{request_hash}:fallback"
            if fallback_name else None
        )
        self.store.record_action_authorization(
            project_id=project.id, action="build_run", scope=scope, risk=risk.value,
            decision="approved", actor="local_user_build_button",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        )
        AuthorizationPolicy(self.store).require(ActionRequest(project.id, "build_run", scope, risk))
        if fallback_scope:
            self.store.record_action_authorization(
                project_id=project.id, action="build_run", scope=fallback_scope,
                risk=ActionRisk.EXTERNAL.value, decision="approved",
                actor="local_user_selected_fallback_and_build_button",
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            )
        try:
            orchestrator = build_orchestrator(
                store=self.store, project=project, client=self.client, mode=mode,
                provider_name=provider_name, model=model, authorization_scope=scope,
                fallback_provider_name=fallback_name,
                fallback_authorization_scope=fallback_scope,
            )
        except (OSError, ValueError, ProviderUnavailable) as exc:
            QMessageBox.critical(self, "Build configuration error", str(exc))
            return
        worker = OrchestrationWorker(orchestrator, request, mode)
        orchestrator.on_event = worker.relay_event
        worker.event.connect(self._build_event)
        worker.completed.connect(self._build_completed)
        worker.failed.connect(self._build_failed)
        worker.finished.connect(lambda: self._orchestration_worker_finished(worker))
        self.orchestration_worker = worker
        self.build_events.clear()
        self.build_details.clear()
        self.last_run_log_path = None
        self.build_log_button.setEnabled(False)
        self.build_merge_button.setEnabled(False)
        self.build_request.setEnabled(False)
        self.build_owner.setEnabled(False)
        self.build_mode.setEnabled(False)
        self.build_provider.setEnabled(False)
        self.build_fallback_provider.setEnabled(False)
        self.build_pause_button.setEnabled(True)
        self.build_cancel_button.setEnabled(True)
        self._refresh_build_readiness()
        self._build_started_at = time.monotonic()
        self._build_activity_frame = 0
        self._build_activity_timer.start()
        self._update_build_activity()
        worker.start()

    def _open_kandev_board(self) -> None:
        try:
            url = kandev_board_url()
        except ValueError as exc:
            QMessageBox.warning(self, "Kandev board", str(exc))
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "Kandev board", f"Could not open {url}. Check that Kandev is running.")

    def _confirm_directory_creation(self, folder: Path) -> ControllerResult:
        if folder.exists():
            return ControllerResult("directory_creation", f"Folder already exists: {folder}", evidence=str(folder))
        if QMessageBox.question(
            self, "Create folder", f"Create this folder on your computer?\n\n{folder}",
        ) != QMessageBox.StandardButton.Yes:
            return ControllerResult(
                "directory_creation", f"Folder creation cancelled: {folder}",
                evidence=str(folder), action_state="cancelled",
            )
        scope = str(folder)
        self.store.record_action_authorization(
            project_id=None, action="create_directory", scope=scope,
            risk=ActionRisk.MUTATION.value, decision="approved",
            actor="local_user_confirmed_exact_directory",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        AuthorizationPolicy(self.store).require(ActionRequest(
            None, "create_directory", scope, ActionRisk.MUTATION,
        ))
        try:
            folder.mkdir()
        except OSError as exc:
            return ControllerResult(
                "directory_creation", f"Could not create folder {folder}: {exc}",
                evidence=str(folder), action_state="failed",
            )
        return ControllerResult("directory_creation", f"Created folder: {folder}", evidence=str(folder))

    def _run_kandev_build(self, project: Project, request: str) -> None:
        try:
            adapter = configured_kandev_adapter(self.store)
        except ValueError as exc:
            QMessageBox.warning(self, "Kandev configuration", str(exc))
            return
        if QMessageBox.question(
            self, "Start Kandev-owned Build",
            f"Create and launch a Kandev task for {project.name} through {adapter.connection.endpoint}? "
            "Kandev will own the agent session. Review its result before applying changes.",
        ) != QMessageBox.StandardButton.Yes:
            return
        config = adapter.api.config

        def authorize_launch(ref) -> None:
            expiry = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            for action, scope in (
                ("kandev_task_create", f"{config.workspace_id}:{ref.external_id}"),
                ("kandev_agent_launch", f"{config.workspace_id}:{ref.external_id}:{config.agent_profile_id}"),
            ):
                self.store.record_action_authorization(
                    project_id=None, action=action, scope=scope, risk=ActionRisk.EXTERNAL.value,
                    decision="approved", actor="local_user_kandev_build_button", expires_at=expiry,
                )

        try:
            orchestrator = build_orchestrator(
                store=self.store, project=project, client=self.client,
                mode=str(self.build_strategy.currentData() or "HYBRID"),
                kandev_connection=adapter.connection, kandev_adapter=adapter,
                kandev_authorize_launch=authorize_launch,
            )
        except (OSError, ValueError, ProviderUnavailable) as exc:
            QMessageBox.critical(self, "Kandev Build configuration", str(exc))
            return
        worker = OrchestrationWorker(orchestrator, request, str(self.build_strategy.currentData() or "HYBRID"))
        orchestrator.on_event = worker.relay_event
        worker.event.connect(self._build_event)
        worker.completed.connect(self._build_completed)
        worker.failed.connect(self._build_failed)
        worker.finished.connect(lambda: self._orchestration_worker_finished(worker))
        self.orchestration_worker = worker
        self.build_events.clear()
        self.build_details.clear()
        self.last_run_log_path = None
        self.build_log_button.setEnabled(False)
        self.build_merge_button.setEnabled(False)
        self.build_request.setEnabled(False)
        self.build_owner.setEnabled(False)
        self.build_mode.setEnabled(False)
        self.build_provider.setEnabled(False)
        self.build_fallback_provider.setEnabled(False)
        self.build_pause_button.setEnabled(False)
        self.build_cancel_button.setEnabled(False)
        self._build_started_at = time.monotonic()
        self._build_activity_timer.start()
        self._refresh_build_readiness()
        worker.start()

    def _update_build_activity(self) -> None:
        if self.orchestration_worker is None:
            self.build_activity.setText("Idle")
            return
        frames = ("◐", "◓", "◑", "◒")
        elapsed = max(0, int(time.monotonic() - self._build_started_at))
        self.build_activity.setText(
            f"{frames[self._build_activity_frame % len(frames)]} Working · {elapsed // 60:02d}:{elapsed % 60:02d} elapsed"
        )
        self._build_activity_frame += 1

    def _build_event(self, kind: str, payload: dict) -> None:
        if kind == "provider_heartbeat":
            elapsed = float(payload.get("elapsed_seconds", 0) or 0)
            self.build_activity.setText(
                f"◐ {str(payload.get('provider', 'Provider')).title()} is still working · {elapsed:.0f}s"
            )
            return
        labels = {
            "run_started": "Run started",
            "recon_completed": "Repository evidence collected",
            "plan_created": "Plan validated",
            "task_started": "Task started",
            "task_completed": "Task completed",
            "task_failed": "Task needs attention",
            "integration_started": "Integration started",
            "integration_conflict": "Integration conflict detected",
            "integration_completed": "Integration validated",
            "integration_reviewed": "Frontier review completed",
            "run_completed": "Run completed",
            "run_failed": "Run failed",
            "run_cancelled": "Run cancelled",
            "external_call": "External consultant call completed",
            "run_stage": "Run stage",
            "workspace_created": "Isolated workspace ready",
            "provider_started": "Provider started",
            "provider_output": "Provider activity",
            "provider_heartbeat": "Provider is still working",
            "provider_finished": "Provider finished",
            "provider_attempt_started": "Implementation attempt started",
            "local_attempt_started": "Bounded local attempt started",
            "provider_takeover": "Frontier provider took over",
            "validation_started": "Validation started",
            "validation_finished": "Validation finished",
            "repair_started": "Validator-guided repair started",
            "changes_applied": "Validated changes applied",
            "run_needs_attention": "Run needs attention",
            "routing_decision": "Route decision",
            "routing_usage": "Route usage",
        }
        detail = ""
        if kind == "plan_created":
            detail = f" · {payload.get('tasks', 0)} task(s)"
        elif kind in {"task_started", "task_completed", "task_failed"}:
            detail = f" · {payload.get('title') or payload.get('key', '')}"
        elif payload.get("error"):
            detail = f" · {payload['error']}"
        elif kind == "run_stage":
            detail = f" · {payload.get('message', payload.get('stage', ''))}"
        elif kind in {"provider_attempt_started", "validation_started", "validation_finished"}:
            detail = f" · {payload.get('key', '')} · attempt {payload.get('attempt', 1)}"
        if kind == "task_failed" and payload.get("reason"):
            detail += f" · {payload['reason']}"
        item = QListWidgetItem(f"{labels.get(kind, kind.replace('_', ' ').title())}{detail}")
        item.setData(Qt.ItemDataRole.UserRole, {"kind": kind, "payload": payload})
        self.build_events.addItem(item)
        self.build_events.scrollToBottom()
        if kind == "run_started" and payload.get("log_path"):
            self.last_run_log_path = Path(str(payload["log_path"]))
            self.build_log_button.setEnabled(True)
        if kind in {"task_failed", "run_failed", "run_cancelled", "run_needs_attention"}:
            self.build_events.setCurrentItem(item)
            self._show_build_event_details(kind, payload)
        if kind == "integration_reviewed" and payload.get("review"):
            self.build_details.setPlainText(str(payload["review"]))
        if kind == "run_stage":
            self.build_activity.setText(f"◐ {payload.get('message', payload.get('stage', 'Working'))}")

    def _build_event_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        payload = data.get("payload")
        if isinstance(payload, dict):
            self._show_build_event_details(str(data.get("kind", "event")), payload)

    def _show_build_event_details(self, kind: str, payload: dict) -> None:
        labels = {
            "run_started": "Run started",
            "task_failed": "Task failed",
            "run_failed": "Run failed",
            "run_cancelled": "Run cancelled",
        }
        lines = [labels.get(kind, kind.replace("_", " ").title())]
        if payload.get("title"):
            lines.append(f"Task: {payload['title']} ({payload.get('key', 'unknown')})")
        elif payload.get("key"):
            lines.append(f"Task: {payload['key']}")
        if payload.get("reason"):
            lines.append(f"Reason: {payload['reason']}")
        if kind == "routing_decision":
            lines.append(f"Selected: {payload.get('selected', 'unknown')}")
            lines.extend(f"Constraint: {item}" for item in payload.get("constraints", []))
            lines.extend(f"Alternative: {item}" for item in payload.get("alternatives", []))
            lines.append(f"Usage accuracy: {payload.get('usage_accuracy', 'unknown')}")
        if kind == "routing_usage":
            lines.append(
                f"Local inference: {payload.get('local_requests', 0)} request(s), "
                f"{payload.get('local_input_tokens', 0)} input / "
                f"{payload.get('local_output_tokens', 0)} output tokens"
            )
            lines.append(
                f"Opaque CLI: {payload.get('opaque_cli_calls', 0)} call(s), "
                f"{payload.get('opaque_cli_reported_input_tokens', 0)} reported input / "
                f"{payload.get('opaque_cli_reported_output_tokens', 0)} reported output tokens; "
                "cost unknown"
            )
        if payload.get("executor_error"):
            lines.append(f"Executor error: {payload['executor_error']}")
        if "turns" in payload or "tool_calls" in payload:
            lines.append(
                f"Model activity: {int(payload.get('turns', 0)):,} turns · "
                f"{int(payload.get('tool_calls', 0)):,} tool calls"
            )
        if "prompt_tokens" in payload or "output_tokens" in payload:
            lines.append(
                f"Tokens: {int(payload.get('prompt_tokens', 0)):,} prompt · "
                f"{int(payload.get('output_tokens', 0)):,} output"
            )
        if payload.get("worktree"):
            lines.append(f"Worktree: {payload['worktree']}")
        trace = payload.get("trace")
        if isinstance(trace, (list, tuple)) and trace:
            lines.extend(["", "Executor trace:"])
            lines.extend(f"  {entry}" for entry in trace)
        if self.last_run_log_path is not None:
            lines.extend(["", f"Run log: {self.last_run_log_path}"])
        self.build_details.setPlainText("\n".join(lines))

    def _open_build_log(self) -> None:
        if self.last_run_log_path is None:
            return
        if not self.last_run_log_path.is_file():
            QMessageBox.information(
                self, "Run log not found", f"The run log does not exist yet:\n{self.last_run_log_path}"
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_run_log_path)))

    def _toggle_build_pause(self) -> None:
        worker = self.orchestration_worker
        if worker is None:
            return
        if worker.paused:
            worker.resume()
            self.build_pause_button.setText("Pause after step")
            self.build_status.setText("Build resumed. Changes take effect at orchestration boundaries.")
        else:
            worker.pause()
            self.build_pause_button.setText("Resume")
            self.build_status.setText("Pause requested. The active model or validation step will finish first.")

    def _cancel_build(self) -> None:
        worker = self.orchestration_worker
        if worker is None:
            return
        worker.cancel()
        self.build_cancel_button.setEnabled(False)
        self.build_status.setText("Stopping the active provider process and preserving the isolated worktree…")

    def _build_completed(self, result) -> None:
        self.last_run_id = result.run_id
        recorded_run = self.store.get_run(result.run_id)
        kandev_owned = bool(recorded_run and recorded_run.get("provider") == "kandev")
        project = self.registry.selected()
        metrics = RunAnalytics(self.store).project_summary(project.id) if project else None
        usage = (
            f"\nHistory: {metrics.completed_runs}/{metrics.total_runs} runs completed · "
            f"{metrics.local_worker_runs} local workers · {metrics.external_calls} external calls"
            if metrics else ""
        )
        if result.status == "completed":
            self.last_integration_branch = result.integration_branch
            self.last_integration_path = result.integration_path
            self.build_merge_button.setEnabled(False)
            self.build_status.setText(
                f"Completed run {result.run_id} · {result.completed_tasks}/{result.total_tasks} tasks passed validation.\n"
                f"Validated changes were applied automatically. Workspace: {result.integration_path}{usage}"
            )
            self._show_integration_diff(result.integration_path, result.base_commit)
        elif result.status == "ready_to_apply":
            self.last_integration_branch = result.integration_branch
            self.last_integration_path = result.integration_path
            self.build_merge_button.setEnabled(bool(result.integration_branch))
            self.build_merge_button.setText("Apply accepted Kandev result" if kandev_owned else "Apply ready result")
            self.build_status.setText(
                (f"Kandev run {result.run_id} passed local validation. Review and apply when ready. {result.error}{usage}"
                 if kandev_owned else
                 f"Run {result.run_id} passed validation but the checkout changed. Review and apply when ready. {result.error}{usage}")
            )
            self._show_integration_diff(result.integration_path, result.base_commit)
        else:
            self.build_status.setText(
                (f"Kandev owns run {result.run_id} · {result.status}. Reconcile its saved external ID to refresh the outcome. "
                 f"{result.error}{usage}" if kandev_owned else
                 f"Run {result.run_id} stopped · {result.completed_tasks}/{result.total_tasks} tasks completed. {result.error}{usage}")
            )
        self._refresh_recovery_actions(project)

    def _build_failed(self, error: str) -> None:
        self.build_status.setText(f"Build worker failed: {error}")
        self.build_events.addItem(f"Worker failure · {error}")

    def _orchestration_worker_finished(self, worker: OrchestrationWorker) -> None:
        worker.deleteLater()
        if self.orchestration_worker is worker:
            self.orchestration_worker = None
        self.build_request.setEnabled(self.registry.selected() is not None)
        self.build_owner.setEnabled(True)
        self.build_mode.setEnabled(True)
        self.build_provider.setEnabled(True)
        self.build_fallback_provider.setEnabled(True)
        self.build_pause_button.setEnabled(False)
        self.build_pause_button.setText("Pause after step")
        self.build_cancel_button.setEnabled(False)
        self._build_activity_timer.stop()
        self.build_activity.setText("Idle")
        self._refresh_recovery_actions(self.registry.selected())
        terminal_status = self.build_status.text()
        self._refresh_build_readiness()
        if not self.build_run_button.isEnabled():
            terminal_status += f"\nNext run: {self.build_status.text()}"
        self.build_status.setText(terminal_status)

    def _show_integration_diff(self, integration_path: Path | None, base_commit: str = "") -> None:
        project = self.registry.selected()
        if integration_path is None or project is None:
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        base = base_commit or subprocess.run(
            ["git", "-C", project.path, "rev-parse", "HEAD^"],
            capture_output=True, text=True, check=False, creationflags=creationflags,
        ).stdout.strip()
        if not base:
            return
        diff = subprocess.run(
            ["git", "-C", str(integration_path), "diff", "--stat", base, "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, creationflags=creationflags,
        ).stdout
        patch = subprocess.run(
            ["git", "-C", str(integration_path), "diff", "--unified=3", base, "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, creationflags=creationflags,
        ).stdout
        self.build_details.setPlainText((diff + "\n" + patch)[:100000])

    def _merge_last_build(self) -> None:
        project = self.registry.selected()
        recorded_run = self.store.get_run(self.last_run_id) if self.last_run_id else None
        if project and recorded_run and recorded_run.get("provider") == "kandev":
            if recorded_run["project_id"] != project.id or recorded_run["status"] != "ready_to_apply":
                return
            if QMessageBox.question(
                self, "Apply accepted Kandev result",
                f"Fast-forward the locally validated checkpoint from Kandev run {self.last_run_id}?",
            ) != QMessageBox.StandardButton.Yes:
                return
            try:
                adapter = configured_kandev_adapter(self.store)
                orchestrator = build_orchestrator(
                    store=self.store, project=project, client=self.client,
                    kandev_connection=adapter.connection, kandev_adapter=adapter,
                )
                result = orchestrator.apply(self.last_run_id)
            except (OSError, ValueError, RuntimeError, ProviderUnavailable) as exc:
                QMessageBox.critical(self, "Kandev apply failed", str(exc))
                return
            self._build_completed(result)
            self._load_project_list(select_id=project.id)
            return
        branch = self.last_integration_branch
        if project is None or not branch:
            return
        state = read_git_state(project.path)
        if not state.available or state.dirty:
            QMessageBox.warning(self, "Cannot merge", "The project checkout must be a clean named branch before merging.")
            return
        if QMessageBox.question(
            self,
            "Merge validated build",
            f"Fast-forward the validated branch {branch} into {state.branch}?",
        ) != QMessageBox.StandardButton.Yes:
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        merged = subprocess.run(
            ["git", "-C", project.path, "-c", "user.name=MoliCoder", "-c", "user.email=molicoder@localhost",
             "merge", "--ff-only", branch],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, creationflags=creationflags,
        )
        if merged.returncode != 0:
            subprocess.run(
                ["git", "-C", project.path, "merge", "--abort"],
                capture_output=True, check=False, creationflags=creationflags,
            )
            QMessageBox.critical(self, "Merge failed", (merged.stdout + merged.stderr)[-4000:])
            return
        self.build_merge_button.setEnabled(False)
        self.build_status.setText(f"Applied validated branch {branch} to {state.branch}. Review and push manually when ready.")
        self._load_project_list(select_id=project.id)

    def _add_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add local project")
        if not path:
            return
        try:
            self._add_project_path(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not add project", str(exc))

    def _create_project(self) -> None:
        parent = QFileDialog.getExistingDirectory(self, "Choose where to create the project")
        if not parent:
            return
        name, accepted = QInputDialog.getText(self, "New project", "Project name")
        if not accepted or not name.strip():
            return
        language, accepted = QInputDialog.getItem(
            self,
            "New project",
            "Language",
            ["Python", "Node.js", "Flutter"],
            0,
            False,
        )
        if not accepted:
            return
        kinds = {"Python": "python", "Node.js": "node", "Flutter": "flutter"}
        try:
            self.build_status.setText("Creating the project, local environment, and initial Git commit…")
            QApplication.processEvents()
            self._create_project_at(parent, name, kinds[language])
        except (OSError, ValueError) as exc:
            self._refresh_build_readiness()
            QMessageBox.critical(self, "Could not create project", str(exc))

    def _create_project_at(self, parent: str | Path, name: str, kind: str) -> Project:
        project = self.registry.create(parent, name, kind)
        self._load_project_list(select_id=project.id)
        self._set_view("build")
        return project

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

    def _recycle_project(self) -> None:
        item = self.project_list.currentItem()
        if item is None:
            return
        project = self.registry.get(int(item.data(Qt.ItemDataRole.UserRole)))
        if project is None:
            return
        if self.orchestration_worker is not None or self.store.has_active_run(project.id):
            QMessageBox.warning(self, "Project is active", "Cancel or finish the active build before deleting this project.")
            return
        path = Path(project.path).resolve()
        if not path.is_dir():
            QMessageBox.warning(self, "Folder missing", f"The project folder does not exist:\n{path}")
            return
        if path == Path(path.anchor) or path == Path.home().resolve():
            QMessageBox.critical(self, "Unsafe project path", f"MoliCoder will not delete this broad path:\n{path}")
            return
        typed, ok = QInputDialog.getText(
            self, "Move project to Recycle Bin",
            f"Type {project.name} to move this exact folder to the Recycle Bin:\n{path}",
        )
        if not ok or typed.strip() != project.name:
            return
        moved = QFile.moveToTrash(str(path))
        success = bool(moved[0]) if isinstance(moved, tuple) else bool(moved)
        if not success:
            QMessageBox.critical(self, "Trash failed", f"The system could not move this folder to Trash:\n{path}")
            return
        self.registry.remove(project.id)
        self.current_project_id = None
        self._load_project_list(select_saved=True)
        QMessageBox.information(self, "Project moved to Trash", "The folder was moved to the system Trash and can be restored there.")

    def _clear_project_history(self) -> None:
        project = self.registry.selected()
        if project is None:
            return
        if self.orchestration_worker is not None or self.store.has_active_run(project.id):
            QMessageBox.warning(self, "Project is active", "Finish or cancel the active build first.")
            return
        if QMessageBox.question(
            self, "Clear build history",
            f"Permanently delete MoliCoder's saved run history for {project.name}? Project files are not removed.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.store.clear_project_history(project.id)
        QMessageBox.information(self, "History cleared", "Saved build history was deleted. Project files were not changed.")

    def _clean_failed_worktrees(self) -> None:
        project = self.registry.selected()
        if project is None:
            return
        if self.orchestration_worker is not None or self.store.has_active_run(project.id):
            QMessageBox.warning(self, "Project is active", "Finish or cancel the active build first.")
            return
        managed_root = (data_dir() / "worktrees").resolve()
        candidates: list[tuple[Path, str]] = []
        for run in self.store.list_project_runs(project.id):
            if run["status"] not in {"failed", "cancelled", "needs_attention"} or not run.get("worktree"):
                continue
            path = Path(str(run["worktree"])).resolve()
            try:
                path.relative_to(managed_root)
            except ValueError:
                continue
            candidates.append((path, f"molicoder/run-{run['id']}"))
        if not candidates:
            QMessageBox.information(self, "No worktrees", "There are no verified failed run worktrees to clean.")
            return
        if QMessageBox.question(
            self, "Clean failed worktrees",
            f"Remove {len(candidates)} failed/cancelled worktree(s) under:\n{managed_root}?\nRun logs and history are retained.",
        ) != QMessageBox.StandardButton.Yes:
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        removed = 0
        for path, branch in candidates:
            subprocess.run(
                ["git", "-C", project.path, "worktree", "remove", "--force", str(path)],
                capture_output=True, check=False, creationflags=creationflags,
            )
            if path.exists():
                QDir(str(path)).removeRecursively()
            subprocess.run(
                ["git", "-C", project.path, "branch", "-D", branch],
                capture_output=True, check=False, creationflags=creationflags,
            )
            removed += int(not path.exists())
        QMessageBox.information(self, "Cleanup complete", f"Removed {removed} verified managed worktree(s).")

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
        if self.current_project_id is not None:
            self.store.link_conversation_project(conversation.id, self.current_project_id)
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
        linked_project_id = self.store.get_conversation_project_id(conversation.id)
        if linked_project_id is not None and linked_project_id != self.current_project_id:
            self._load_project_list(select_id=linked_project_id)
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
        deterministic_question = not self.current_attachments and self.controller.can_handle_locally(
            self.composer.toPlainText()
        )
        can_send = bool(self.composer.toPlainText().strip() or self.current_attachments) and (deterministic_question or not budget.blocked)
        can_send = can_send and self.chat_worker is None
        if self._installed_model_names and not deterministic_question:
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
        linked_project_id = self.store.get_conversation_project_id(self.current_conversation_id)
        if self.current_project_id is not None and linked_project_id != self.current_project_id:
            if linked_project_id is not None:
                self._new_chat()
            else:
                self.store.link_conversation_project(self.current_conversation_id, self.current_project_id)
        content = self._pending_content()
        if not content.strip():
            return
        if not self.current_attachments:
            result = self.controller.handle(self.composer.toPlainText(), self.current_project_id)
            if result is not None:
                if result.kind == "directory_creation_proposal" and result.evidence:
                    result = self._confirm_directory_creation(Path(result.evidence))
                self._send_controller_reply(content, result)
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
        self.chat_worker = ChatWorker(
            self.client, self.preset.name, api_messages, options,
            self.inference_policy, self.current_project_id,
        )
        self.chat_worker.content_delta.connect(self._append_content)
        self.chat_worker.thinking_delta.connect(self._append_thinking)
        self.chat_worker.completed.connect(self._generation_finished)
        self.chat_worker.failed.connect(self._generation_failed)
        self.chat_worker.start()
        self.send_button.setVisible(False)
        self.stop_button.setVisible(True)
        self.model_combo.setEnabled(False)
        self._refresh_context_meter()

    def _send_controller_reply(self, content: str, result: ControllerResult) -> None:
        conversation_id = self.current_conversation_id
        if conversation_id is None:
            return
        self.store.add_message(Message(None, conversation_id, "user", content))
        self.store.add_message(Message(None, conversation_id, "assistant", result.content))
        if result.kind == "execution_proposal" and result.action_state in {"awaiting_approval", "awaiting_clarification"}:
            self.pending_build_request = (self.current_project_id, content)
            self._update_pending_build_card()
        conversation = self.store.get_conversation(conversation_id)
        if conversation and conversation.title == "New chat":
            self.store.rename_conversation(conversation_id, " ".join(content.split())[:56])
        self.composer.clear()
        self._load_conversation_list(select_id=conversation_id)
        self._refresh_context_meter()

    def _update_pending_build_card(self) -> None:
        pending = self.pending_build_request
        self.pending_build_card.setVisible(pending is not None)
        if pending is None:
            return
        target_id = pending[0] if pending[0] is not None else self.current_project_id
        project = self.store.get_project(target_id) if target_id is not None else None
        name = project.name if project else "select a project"
        self.pending_build_label.setText(f"Build proposal for {name}: {pending[1][:120]}")
        self.pending_build_review.setEnabled(project is not None)

    def _review_pending_build(self) -> None:
        pending = self.pending_build_request
        if pending is None:
            return
        target_id = pending[0] if pending[0] is not None else self.current_project_id
        project = self.store.get_project(target_id) if target_id is not None else None
        if project is None:
            self._update_pending_build_card()
            return
        self._load_project_list(select_id=project.id)
        self.build_request.setPlainText(pending[1])
        self._set_view("build")
        self.pending_build_request = None
        self._update_pending_build_card()

    def _cancel_pending_build(self) -> None:
        self.pending_build_request = None
        self._update_pending_build_card()

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
        self._refresh_pacing()
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

    def _refresh_pacing(self) -> None:
        if self.current_project_id is None:
            self.pacing_label.setText("Paid API pacing: select a project.")
            return
        self.pacing_label.setText(project_pacing(self.store, self.current_project_id).explanation)

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
        if self.orchestration_worker and self.orchestration_worker.isRunning():
            QMessageBox.warning(
                self,
                "Build still running",
                "Wait for the current validated task to finish before closing MoliCoder.",
            )
            event.ignore()
            return
        if self.chat_worker:
            self.chat_worker.cancel()
            self.chat_worker.wait(2500)
        if self.startup_worker and self.startup_worker.isRunning():
            self.startup_worker.wait(3000)
        event.accept()
