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
    w.setWindowIcon(QIcon(str(logo_path.with_name('MoliCoder.ico'))))
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
    w.history_actions = QListWidget(); w.history_tabs.addTab(w.history_actions, 'Local actions')
    w.history_actions.itemClicked.connect(lambda item: (w.build_details.setPlainText(item.data(Qt.UserRole)), w.history_dialog.hide(), open_drawer(w, 'Activity')))
    w.history_runs.itemClicked.connect(lambda item: show_run(w, item.data(Qt.UserRole)))
    history_layout.addWidget(w.history_tabs)
    w.sidebar.setMaximumWidth(500)
    row.addWidget(w.chat_nav_button); row.addWidget(w.build_nav_button)
    w.new_chat_button = button('New chat', lambda: w._open_new_chat())
    row.addWidget(w.new_chat_button)
    row.addStretch()
    row.addWidget(QLabel('Theme'))
    w.theme_selector = QComboBox()
    w.theme_selector.setAccessibleName('Theme')
    w.theme_selector.addItem('Matrix', 'matrix')
    w.theme_selector.addItem('Light', 'light')
    w.theme_selector.currentIndexChanged.connect(lambda: select_theme(w))
    row.addWidget(w.theme_selector)
    row.addWidget(button('History', lambda: show_history(w)))
    row.addWidget(button('Settings', lambda: show_settings(w)))
    layout.addWidget(bar)
    strip = QFrame(); strip.setObjectName('usageStrip')
    strip_row = QHBoxLayout(strip); strip_row.setContentsMargins(22, 8, 22, 8)
    w.route_identity = QLabel()
    w.route_identity.setObjectName('routeIdentity')
    w.route_identity.setAccessibleName('Provider and model')
    w.route_identity.setWordWrap(True)
    w.route_identity.setTextInteractionFlags(Qt.TextSelectableByMouse)
    w.route_identity.setStyleSheet('font-weight: 650; font-size: 18px;')
    w.route_identity.setTextFormat(Qt.PlainText)
    w.route_identity.setContentsMargins(22, 8, 22, 0)
    layout.addWidget(w.route_identity)
    w.work_feedback = QLabel()
    w.work_feedback.setWordWrap(True)
    w.work_feedback.setContentsMargins(22, 8, 22, 8)
    w.work_feedback.setTextFormat(Qt.PlainText)
    w.work_feedback.hide()
    layout.addWidget(w.work_feedback)
    w.failure_recovery = button('Review failure', lambda: recover_failure(w))
    w.failure_recovery.hide()
    layout.addWidget(w.failure_recovery)
    from .activity_indicator import ActivityIndicator
    activity_row = QFrame()
    activity_layout = QHBoxLayout(activity_row)
    activity_layout.setContentsMargins(22, 4, 22, 4)
    w.busy_indicator = ActivityIndicator()
    w.current_activity = QLabel('')
    w.current_activity.setWordWrap(True)
    activity_layout.addWidget(w.busy_indicator)
    activity_layout.addWidget(w.current_activity, 1)
    w.return_to_build = button('Open active Build', lambda: w._set_view('build'))
    activity_layout.addWidget(w.return_to_build)
    w.activity_row = activity_row
    activity_row.hide()
    layout.addWidget(activity_row)
    w.connection_label = QLabel('Local runtime: Ollama · checking')
    w.center = w._build_center()
    w.inspector = w._build_inspector()
    w.inspector.layout().insertWidget(1, w.connection_label)
    w.context_summary = QLabel('New conversation · No context sent yet')
    w.context_summary.setWordWrap(True)
    strip_row.addWidget(w.context_summary, 1)
    w.usage_summary = QLabel('Local route · no API token charge')
    w.usage_summary.setWordWrap(True); strip_row.addWidget(w.usage_summary, 1)
    w.inspector_button = button('Usage ↗', lambda: open_drawer(w, 'Usage'))
    strip_row.addWidget(w.inspector_button)
    strip_row.addWidget(button('Change model', lambda: show_settings(w, 'Models & providers')))
    strip_row.addWidget(button('Context details', lambda: show_context_details(w)))
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
    from .route_identity import refresh_route_identity
    refresh_route_identity(w)


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
        w.registry.clear_selection()
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
    w.project_switch.addItem('No project', None)
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


def show_settings(w, category='General'):
    from .settings_window import show_settings_window
    show_settings_window(w, category)


def show_context_details(w):
    import json
    receipt = last_receipt(w)
    dialog = QDialog(w)
    dialog.setWindowTitle('Context actually included')
    dialog.resize(650, 450)
    layout = QVBoxLayout(dialog)
    text = QTextBrowser()
    text.setPlainText(json.dumps(receipt, indent=2) if receipt else 'No receipt available for this conversation yet. Older replies may not have recorded attribution.')
    layout.addWidget(text)
    layout.addWidget(button('Close', dialog.accept))
    dialog.exec()


def last_receipt(w):
    messages = w.store.list_messages(w.current_conversation_id) if w.current_conversation_id else []
    last = next((message for message in reversed(messages) if message.role == 'assistant'), None)
    return w.store.message_receipt(last.id) if last else None


def legacy_settings(w):
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
    refresh_activity(w)
    from .route_identity import refresh_route_identity
    refresh_route_identity(w)
    sync_projects(w)
    w.project_switch.setEnabled(w.orchestration_worker is None)
    w.project_switch.setToolTip('The active Build owns its target until it stops.' if w.orchestration_worker is not None else 'Choose an explicit project, or No project for general Chat.')
    w.project_list.setEnabled(w.orchestration_worker is None)
    build = w.view_stack.currentWidget() == w.build_view
    w.build_nav_button.setProperty('mode', 'build' if build else '')
    if build:
        project = w.registry.selected()
        w.context_summary.setText(f'Target: {project.name} · {project.path}' if project else 'No project · Choose a folder for repository work')
        status = w.store.current_project_run_status(project.id) if project else None
        if not status or not status.run_id:
            w.usage_summary.setText('Build route · provider cost unknown')
        if status and status.run_id:
            run = w.store.get_run(status.run_id)
            from ..work_state import WorkSnapshot
            w.workspace_state.work = WorkSnapshot.from_run(w.store, status.run_id)
            snapshot = w.workspace_state.work
            verified = sum(task.status == 'completed' for task in status.tasks)
            w.context_summary.setText(f'Run #{status.run_id} · {verified}/{len(status.tasks)} tasks verified · {snapshot.phase} · {project.name}')
            failed = snapshot.outcome in {'failed', 'needs_attention', 'interrupted'}
            w.work_feedback.setVisible(failed)
            if failed:
                present_failure(w, snapshot.reason or 'Open Activity for details.')
            else:
                w.failure_recovery.hide()
            from contextlib import closing
            with closing(w.store._connect()) as connection:
                usage = connection.execute("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM external_calls WHERE run_id=?", (status.run_id,)).fetchone()
            import json
            calls = [json.loads(event.payload_json) for event in w.store.list_events(status.run_id) if event.kind == 'provider_finished']
            inputs = [call['input_tokens'] for call in calls if call.get('input_tokens') is not None]
            outputs = [call['output_tokens'] for call in calls if call.get('output_tokens') is not None]
            w.usage_summary.setText(f"{usage[0]} CLI calls · reported input {sum(inputs) if inputs else 'unknown'} / output {sum(outputs) if outputs else 'unknown'} · cost unknown")
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
        w.work_feedback.hide()
        w.failure_recovery.hide()
        receipt = last_receipt(w)
        if receipt and receipt.get('model_source') == 'no_inference':
            w.context_summary.setText('Local records/action · No AI model used')
            w.usage_summary.setText('No inference token usage for this reply')
        elif receipt:
            w.context_summary.setText(f"Last reply included: {len(receipt.get('turn_ids', []))} conversation turns · {len(receipt.get('attachments', []))} attachments")
            metrics = receipt.get('metrics', {})
            w.usage_summary.setText(f"Last reply: {metrics.get('prompt_tokens') or 'unknown'} input / {metrics.get('output_tokens') or 'unknown'} output tokens")
        else:
            w.context_summary.setText('No recorded request context yet')
            w.usage_summary.setText('Usage available after a measured reply')

    if w.current_project_id:
        from ..inference.budget import BudgetManager
        limit = BudgetManager(w.store).project_limit(w.current_project_id)
        if limit is not None:
            w.usage_summary.setText(w.usage_summary.text() + f" · API limit ${limit:.2f} all time")
    if getattr(w, '_request_failure', ''):
        present_failure(w, w._request_failure)
    action = getattr(w, '_last_local_action', None)
    if build and action and w.orchestration_worker is None:
        w.context_summary.setText(f"Local action target: {action['target']}")
        w.usage_summary.setText('No AI model used')
        w.work_feedback.setText(action['detail'])
        w.work_feedback.show()
        w.route_identity.setText(f"Local action · No AI model used · {action['outcome']}")


def present_failure(w, reason):
    from .failure_feedback import failure_feedback
    title, summary, destination = failure_feedback(reason)
    w._failure_destination = destination
    w.work_feedback.setText(f'{title} · {summary}\nDetails: {reason[:1200]}')
    w.work_feedback.show()
    w.failure_recovery.setText(destination)
    w.failure_recovery.show()


def recover_failure(w):
    destination = getattr(w, '_failure_destination', 'Activity')
    if destination == 'Edit request':
        w._set_view('build'); w.build_request.setFocus()
    elif destination in {'Models & providers', 'Usage & limits'}:
        show_settings(w, destination)
    else:
        open_drawer(w, destination)


def refresh_activity(w):
    import time
    if not hasattr(w, 'busy_indicator'):
        return
    active = w.orchestration_worker is not None and not getattr(w, '_build_terminal', False)
    pause_state = getattr(w.orchestration_worker, 'pause_state', 'running')
    waiting = active and pause_state == 'paused'
    reduced = w.store.get_setting('ui.reduce_motion', 'false') == 'true'
    w.busy_indicator.set_busy(active and not waiting, reduced)
    w.activity_row.setVisible(active)
    w.return_to_build.setVisible(active and w.view_stack.currentWidget() != w.build_view)
    if active:
        elapsed = max(0, int(time.monotonic() - w._build_started_at))
        since = max(0, int(time.monotonic() - getattr(w, '_last_build_event_at', w._build_started_at)))
        stage = 'Paused' if waiting else getattr(w, '_active_stage_label', 'Preparing request')
        if pause_state == 'pause_requested': stage = 'Pause requested · finishing the current step'
        if pause_state == 'cancelling': stage = 'Cancelling · waiting for the worker to stop'
        suffix = ' · Waiting for provider; no recent update' if since >= 30 else ''
        w.current_activity.setText(f'{stage} · {elapsed}s elapsed · Last activity {since}s ago{suffix}')


def show_history(w):
    from PySide6.QtWidgets import QListWidgetItem
    w.history_runs.clear()
    w.history_actions.clear()
    for action in w.store.list_local_actions():
        item = QListWidgetItem(f"#{action['id']} · {action['outcome']} · {action['target']}")
        item.setData(Qt.UserRole, action['detail'] + '\nNo AI model used · ' + action['created_at'])
        w.history_actions.addItem(item)
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



def select_theme(w):
    from .theme import apply_theme
    apply_theme(w, w.theme_selector.currentData())
