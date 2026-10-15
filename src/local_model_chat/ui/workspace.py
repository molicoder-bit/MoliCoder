"""Horizontal desktop workspace; execution remains in existing controllers."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QAction
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QDialog, QTabWidget, QComboBox, QTextBrowser, QSplitter, QListWidget)


def button(text, callback):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    return widget


def build_workspace(w):
    root = QWidget()
    w.setCentralWidget(root)
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    bar = QFrame(); bar.setObjectName('topbar')
    row = QHBoxLayout(bar); row.setContentsMargins(20, 12, 20, 12)
    logo_path = Path(__file__).parents[1] / 'assets' / 'molicodeLogo.png'
    w.setWindowIcon(QIcon(str(logo_path)))
    logo = QLabel(); logo.setObjectName('brandLogo'); logo.setFixedSize(46, 46)
    logo.setPixmap(QPixmap(str(logo_path)).scaled(42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    logo.setAlignment(Qt.AlignCenter)
    row.addWidget(logo)
    title = QLabel('MoliCoder'); title.setObjectName('appTitle'); row.addWidget(title)
    w.project_switch = QComboBox(); w.project_switch.setMinimumWidth(150)
    w.project_switch.setAccessibleName('Project selector')
    w.project_switch.setEditable(True)
    w.project_switch.setInsertPolicy(QComboBox.NoInsert)
    w.project_switch.completer().setFilterMode(Qt.MatchContains)
    w.project_switch.completer().setCaseSensitivity(Qt.CaseInsensitive)
    w.project_switch.currentIndexChanged.connect(lambda: select_project(w))
    row.addWidget(w.project_switch)
    w.sidebar = w._build_sidebar()
    w.history_dialog = QDialog(w); w.history_dialog.setWindowTitle('Projects and conversations')
    history_layout = QVBoxLayout(w.history_dialog)
    w.history_tabs = QTabWidget(); w.history_tabs.addTab(w.sidebar, 'Projects / conversations')
    w.history_runs = QListWidget(); w.history_tabs.addTab(w.history_runs, 'Build runs')
    w.history_runs.itemClicked.connect(lambda item: show_run(w, item.data(Qt.UserRole)))
    history_layout.addWidget(w.history_tabs)
    w.sidebar.setMaximumWidth(500)
    row.addWidget(w.chat_nav_button); row.addWidget(w.build_nav_button)
    row.addStretch()
    row.addWidget(button('History', lambda: show_history(w)))
    row.addWidget(button('Settings', lambda: show_settings(w)))
    layout.addWidget(bar)
    strip = QFrame(); strip.setObjectName('usageStrip')
    strip_row = QHBoxLayout(strip); strip_row.setContentsMargins(22, 8, 22, 8)
    w.connection_label = QLabel('Ollama · checking'); strip_row.addWidget(w.connection_label)
    w.center = w._build_center()
    w.inspector = w._build_inspector()
    strip_row.addWidget(w.context_progress)
    w.context_progress.setMaximumWidth(110)
    strip_row.addWidget(w.context_label, 1)
    w.usage_summary = QLabel('Local route · no API token charge')
    w.usage_summary.setWordWrap(True); strip_row.addWidget(w.usage_summary, 1)
    w.inspector_button = button('Usage ↗', lambda: open_drawer(w, 'Usage'))
    strip_row.addWidget(w.inspector_button)
    layout.addWidget(strip)
    layout.addWidget(w.center, 1)
    w.drawer = QTabWidget(); w.drawer.setObjectName('activityDrawer')
    w.drawer.setMaximumHeight(300)
    w.drawer.setMinimumHeight(200)
    activity = QSplitter(Qt.Horizontal)
    activity.addWidget(w.build_events); activity.addWidget(w.build_details)
    activity.setSizes([280, 700])
    w.drawer.addTab(activity, 'Activity')
    w.changes_view = QTextBrowser(); w.drawer.addTab(w.changes_view, 'Changes')
    w.validation_view = QTextBrowser(); w.drawer.addTab(w.validation_view, 'Validation')
    w.usage_view = QTextBrowser(); w.drawer.addTab(w.usage_view, 'Usage')
    w.knowledge_view = QTextBrowser(); w.drawer.addTab(w.knowledge_view, 'Knowledge')
    perf = QScroll(w.inspector); w.drawer.addTab(perf, 'Performance')
    w.drawer.setVisible(False)
    layout.addWidget(w.drawer)
    bottom = QFrame(); bottom.setObjectName('drawerBar')
    bottom_row = QHBoxLayout(bottom); bottom_row.setContentsMargins(18, 4, 18, 4)
    for tab in ('Activity', 'Changes', 'Validation', 'Usage', 'Knowledge', 'Performance'):
        bottom_row.addWidget(button(tab, lambda _checked=False, name=tab: open_drawer(w, name)))
    bottom_row.addStretch()
    bottom_row.addWidget(button('Hide', lambda: w.drawer.hide()))
    layout.addWidget(bottom)
    w.project_list.currentItemChanged.connect(lambda *_: sync_projects(w))
    sync_projects(w)
    for key, target in [('Ctrl+1', 'chat'), ('Ctrl+2', 'build')]:
        action = QAction(w); action.setShortcut(key)
        action.triggered.connect(lambda _checked=False, name=target: w._set_view(name)); w.addAction(action)
    w._workspace_timer = __import__('PySide6.QtCore', fromlist=['QTimer']).QTimer(w)
    w._workspace_timer.timeout.connect(lambda: refresh_workspace(w)); w._workspace_timer.start(1500)


def QScroll(widget):
    from PySide6.QtWidgets import QScrollArea
    scroll = QScrollArea(); scroll.setWidgetResizable(True)
    widget.setMinimumWidth(0); widget.setMaximumWidth(16777215)
    scroll.setWidget(widget)
    return scroll


def select_project(w):
    project_id = w.project_switch.currentData()
    if project_id is None:
        if w.orchestration_worker is not None: return
        w.workspace_state.drafts[w.current_project_id] = (w.composer.toPlainText(), w.build_request.toPlainText(), list(w.current_attachments))
        w.current_project_id = None
        chat, build, attachments = w.workspace_state.drafts.get(None, ('', '', []))
        w.composer.setPlainText(chat); w.build_request.setPlainText(build)
        w.current_attachments = list(attachments); w._render_attachment_chips()
        w.project_list.clearSelection()
        w._update_build_project(None)
        return
    for index in range(w.project_list.count()):
        item = w.project_list.item(index)
        if item.data(Qt.UserRole) == project_id:
            w.project_list.setCurrentItem(item); break


def sync_projects(w):
    signature = (w.current_project_id, tuple((p.id, p.name) for p in w.registry.list()))
    if signature == getattr(w, '_project_signature', None): return
    w._project_signature = signature
    w.project_switch.blockSignals(True); w.project_switch.clear()
    w.project_switch.addItem('Personal workspace', None)
    for project in w.registry.list():
        w.project_switch.addItem(project.name, project.id)
    w.project_switch.setCurrentIndex(max(0, w.project_switch.findData(w.current_project_id)))
    w.project_switch.blockSignals(False)


def open_drawer(w, name):
    if name == 'Usage' and w.current_project_id:
        from ..inference.usage_view import render_usage_report
        w.usage_view.setPlainText(render_usage_report(w.store, w.inference_registry, w.current_project_id))
    if name == 'Knowledge' and w.current_project_id:
        from ..continuity import build_continuity
        summary = build_continuity(w.store, w.current_project_id)
        w.knowledge_view.setPlainText(summary.render() if summary else 'No project knowledge yet.')
    for index in range(w.drawer.count()):
        if w.drawer.tabText(index) == name:
            w.drawer.setCurrentIndex(index); break
    w.drawer.show()


def show_settings(w):
    dialog = QDialog(w); dialog.setWindowTitle('Workspace settings'); dialog.resize(600, 400)
    layout = QVBoxLayout(dialog)
    title = QLabel('Your workspace, your resources'); title.setObjectName('appTitle'); layout.addWidget(title)
    for label, callback in [('Models, installs and environment', w._show_environment_doctor),
            ('Agents, API connections and project trust', w._provider_settings),
            ('Project API budgets and pacing', w._show_usage_budgets),
            ('Project knowledge and approved decisions', w._open_knowledge_review),
            ('Kandev connection and service', lambda: w.kandev_panel.configure()),
            ('Export task evidence and trace', w._show_task_evidence)]:
        layout.addWidget(button(label, lambda _checked=False, action=callback: (dialog.accept(), action())))
    layout.addStretch(); layout.addWidget(button('Done', dialog.accept)); dialog.exec()


def refresh_workspace(w):
    sync_projects(w)
    w.project_switch.setEnabled(w.orchestration_worker is None)
    w.project_list.setEnabled(w.orchestration_worker is None)
    build = w.view_stack.currentWidget() == w.build_view
    w.build_nav_button.setProperty('mode', 'build' if build else '')
    if build:
        project = w.registry.selected()
        status = w.store.current_project_run_status(project.id) if project else None
        if not status or not status.run_id:
            w.usage_summary.setText('Build route · provider cost unknown')
        if status and status.run_id:
            run = w.store.get_run(status.run_id)
            from contextlib import closing
            with closing(w.store._connect()) as connection:
                usage = connection.execute("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM external_calls WHERE run_id=?", (status.run_id,)).fetchone()
            w.usage_summary.setText(f'Run #{status.run_id} · {usage[0]} CLI calls · {usage[1]:,} input / {usage[2]:,} output reported · cost unknown')
            signature = tuple((task.id, task.status, task.title) for task in status.tasks)
            changed = signature != getattr(w, '_task_signature', None)
            if changed: w.task_list.clear()
            w._task_signature = signature
            for task in status.tasks if changed else ():
                from PySide6.QtWidgets import QListWidgetItem
                item = QListWidgetItem(f'{task.key}   {task.title}   ·   {task.status}')
                item.setData(Qt.UserRole, task.id); w.task_list.addItem(item)
            if run and w.orchestration_worker is None:
                w.run_stage.setText(' / '.join(('Prepare', 'Plan', 'Execute', 'Check', 'Apply')) + f'    ·    {run["status"]}')
    else:
        w.usage_summary.setText(f'Last response: {w.last_metrics.prompt_tokens:,} input / {w.last_metrics.output_tokens:,} output · local route')

    if w.current_project_id:
        from ..inference.budget import BudgetManager
        limit = BudgetManager(w.store).project_limit(w.current_project_id)
        if limit is not None:
            w.usage_summary.setText(w.usage_summary.text() + f" · API limit ${limit:.2f} all time")


def show_history(w):
    from PySide6.QtWidgets import QListWidgetItem
    w.history_runs.clear()
    if w.current_project_id:
        for run in w.store.list_project_runs(w.current_project_id):
            item = QListWidgetItem(f"#{run['id']} · {run['status']} · {run['request'][:60]}")
            item.setData(Qt.UserRole, run['id']); w.history_runs.addItem(item)
    w.history_tabs.setCurrentIndex(1 if w.workspace_state.mode == 'build' else 0)
    w.history_dialog.show()


def show_run(w, run_id):
    from ..molicoder.evidence_view import render_task_evidence
    w.validation_view.setPlainText(render_task_evidence(w.store, run_id))
    open_drawer(w, 'Validation')
    w.history_dialog.hide()

