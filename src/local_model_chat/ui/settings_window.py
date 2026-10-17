"""Persistent, navigable settings with everyday actions first."""
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QVBoxLayout, QListWidget,
    QStackedWidget, QWidget, QLabel, QPushButton, QCheckBox, QComboBox, QScrollArea, QSpinBox)
from PySide6.QtCore import QThread, Signal


class ProviderCheck(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            from dataclasses import replace
            from ..molicoder.providers import probe_all_providers
            from ..environment.provider_connection import ProviderConnection
            statuses = []
            for status in probe_all_providers():
                if status.provider == 'codex' and status.installed and not status.auth_verified:
                    check = ProviderConnection('codex')
                    binary = check.runtime.discover_binary()
                    state = check.auth_state(binary) if binary else 'unknown'
                    if state != 'unknown':
                        status = replace(status, authenticated=state == 'signed_in', auth_verified=True,
                            detail='Sign-in checked; use Test connection to verify model access.' if state == 'signed_in' else 'Sign-in required.')
                statuses.append(status)
            self.ready.emit(tuple(statuses))
        except Exception as exc:
            self.failed.emit(str(exc))


class CatalogRefresh(QThread):
    ready = Signal(object)
    failed = Signal(str)
    def __init__(self, store, client, parent):
        super().__init__(parent); self.store, self.client = store, client
    def run(self):
        try:
            from ..orchestration.catalog import CapabilityCatalogService
            self.ready.emit(CapabilityCatalogService(self.store, self.client).refresh())
        except Exception as exc:
            self.failed.emit(str(exc))


def show_catalog(w):
    from PySide6.QtWidgets import QTextBrowser
    from ..orchestration.catalog import CapabilityCatalogService
    import json
    dialog = QDialog(w); dialog.setWindowTitle('Models, capabilities and routing'); dialog.resize(860, 650)
    layout = QVBoxLayout(dialog); status = QLabel(); status.setWordWrap(True); layout.addWidget(status)
    report = QTextBrowser(); layout.addWidget(report)
    refresh = QPushButton('Refresh available models · no generation'); layout.addWidget(refresh)
    def render(snapshot):
        status.setText(('Stale cached catalog · ' if snapshot.get('stale') else 'Catalog refreshed · ') +
            str(snapshot.get('revision') or 'Not refreshed yet') + ('\n' + snapshot['error'] if snapshot.get('error') else ''))
        catalog = CapabilityCatalogService(w.store, w.client)
        routes, excluded = catalog.routes(w.registry.selected(), w.preset.name)
        from dataclasses import asdict
        report.setPlainText(json.dumps({'eligible_request_routes': [asdict(r) for r in routes],
            'excluded_routes': excluded, 'observed_results': catalog.route_records(), 'catalog': snapshot}, indent=2))
    def begin():
        if getattr(w, '_catalog_refresh_worker', None): return
        worker = CatalogRefresh(w.store, w.client, w); w._catalog_refresh_worker = worker
        refresh.setEnabled(False); status.setText('Refreshing model metadata…')
        worker.ready.connect(render); worker.failed.connect(status.setText)
        worker.finished.connect(lambda: (refresh.setEnabled(True), setattr(w, '_catalog_refresh_worker', None), worker.deleteLater()))
        worker.start()
    refresh.clicked.connect(begin); render(CapabilityCatalogService(w.store, w.client).cached()); dialog.show()


CATEGORIES = ('General', 'Models & providers', 'Projects & Build', 'Usage & limits', 'Kandev', 'Diagnostics')


def show_settings_window(w, category='General'):
    if not hasattr(w, '_settings_dialog'):
        dialog = QDialog(w)
        dialog.setWindowTitle('Settings')
        dialog.resize(900, 640)
        layout = QHBoxLayout(dialog)
        nav = QListWidget(); nav.addItems(CATEGORIES); nav.setMaximumWidth(220)
        pages = QStackedWidget()
        layout.addWidget(nav); layout.addWidget(pages, 1)
        descriptions = (
            'App preferences · Changes are saved immediately.',
            'Use local models first, with suitable providers when needed.',
            'Choose a folder for repository work and review how Build runs.',
            'Review measured usage and set project limits. Subscription agent usage may be unavailable.',
            'Connect a local board to display internal Build tasks and live execution state, or configure Kandev execution.',
            'Inspect local runtime health, installation readiness, and run evidence.',
        )
        for name, description in zip(CATEGORIES, descriptions):
            page = QWidget(); body = QVBoxLayout(page)
            title = QLabel(name); title.setObjectName('appTitle'); body.addWidget(title)
            note = QLabel(description); note.setWordWrap(True); body.addWidget(note)
            def action(label, callback):
                button = QPushButton(label); button.clicked.connect(callback); body.addWidget(button)
            if name == 'General':
                theme = QComboBox(); theme.addItems(['Matrix', 'Light'])
                theme.setCurrentIndex(w.theme_selector.currentIndex())
                theme.currentIndexChanged.connect(w.theme_selector.setCurrentIndex)
                w._settings_theme_selector = theme
                body.addWidget(QLabel('Appearance')); body.addWidget(theme)
                from .theme import MAX_TEXT_SIZE, MIN_TEXT_SIZE, current_text_size, set_text_size
                size = QSpinBox(); size.setRange(MIN_TEXT_SIZE, MAX_TEXT_SIZE); size.setSuffix(' px')
                size.setValue(current_text_size(w))
                w._settings_text_size_control = size
                def set_size(value):
                    set_text_size(w, value)
                size.valueChanged.connect(set_size)
                body.addWidget(QLabel('Text size · Entire app')); body.addWidget(size)
                for label, key in [('Remember last workspace on launch', 'ui.remember_workspace'), ('Reduce animation', 'ui.reduce_motion')]:
                    check = QCheckBox(label); check.setChecked(w.store.get_setting(key, 'false') == 'true')
                    check.toggled.connect(lambda value, setting=key: w.store.set_setting(setting, str(value).lower()))
                    body.addWidget(check)
            elif name == 'Models & providers':
                body.addWidget(QLabel('Preferred local model'))
                body.addWidget(w.model_combo); w.model_combo.show()
                w.model_combo.setToolTip('Changing a model can open a new chat. Previous replies keep their recorded origin.')
                local_row = QHBoxLayout(); body.addLayout(local_row)
                health = QLabel(w.connection_label.text()); local_row.addWidget(health, 1)
                install = QPushButton('Install local models…'); install.clicked.connect(w._show_environment_doctor); local_row.addWidget(install)
                w._settings_local_health = health
                from .provider_setup import ProviderPanel
                w._settings_providers = ProviderPanel(w)
                body.addWidget(w._settings_providers)
                advanced_row = QHBoxLayout(); body.addLayout(advanced_row)
                api = QPushButton('API connections…'); api.clicked.connect(w._advanced_api_settings); advanced_row.addWidget(api)
                catalog = QPushButton('Model catalog…'); catalog.clicked.connect(lambda: show_catalog(w)); advanced_row.addWidget(catalog)
            elif name == 'Projects & Build':
                project = w.registry.selected()
                scope = QLabel(f'Project: {project.name}\n{project.path}' if project else 'No project selected'); scope.setWordWrap(True); body.addWidget(scope)
                w._settings_project_summary = scope
                chooser = QComboBox(); chooser.addItem('Choose an existing project', None)
                for item in w.registry.list():
                    chooser.addItem(f'{item.name} · {item.path}', item.id)
                chooser.currentIndexChanged.connect(lambda: w._load_project_list(select_id=chooser.currentData()) if chooser.currentData() is not None and w.orchestration_worker is None else None)
                body.addWidget(chooser)
                action('Use no project', lambda: w.project_switch.setCurrentIndex(w.project_switch.findData(None)) if w.orchestration_worker is None else None)
                action('Open project folder', w._add_project)
                action('Create project', w._create_project)
                action('Remove selected project from list · keeps its folder', w._remove_project)
                action('Project knowledge and decisions', w._open_knowledge_review)
                explanation = QLabel('Local offline uses your local model for eligible edits. Hybrid can hand work to your chosen external agent. Frontier only uses that external agent. Build asks for the required project permission before an agent runs.'); explanation.setWordWrap(True); body.addWidget(explanation)
            elif name == 'Usage & limits':
                explanation = QLabel('Measured tokens are shown when the provider reports them. Estimates are labeled separately; missing measurements remain unknown. A metered API hard limit can block spending, while subscription-agent call/time limits cannot cap the agent’s internal token usage.'); explanation.setWordWrap(True); body.addWidget(explanation)
                action('Review project usage and set limits', w._show_usage_budgets)
            elif name == 'Kandev':
                state = QLabel(w.kandev_panel.status.text()); state.setWordWrap(True); body.addWidget(state)
                w._settings_kandev_status = state
                action('Connect or configure Kandev', w.kandev_panel.configure)
                action('Open board', lambda: (w._set_view('build'), w.build_tabs.setCurrentIndex(1), dialog.hide()))
            else:
                action('Check environment and installation readiness', w._show_environment_doctor)
                action('Export task evidence and logs', w._show_task_evidence)
                action('Inspect local runtime performance', lambda: (dialog.hide(), __import__('local_model_chat.ui.workspace', fromlist=['open_drawer']).open_drawer(w, 'Performance')))
            body.addStretch()
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page); pages.addWidget(scroll)
        nav.currentRowChanged.connect(pages.setCurrentIndex)
        w._settings_dialog, w._settings_nav = dialog, nav
    w._settings_nav.setCurrentRow(CATEGORIES.index(category) if category in CATEGORIES else 0)
    w._settings_theme_selector.blockSignals(True)
    w._settings_theme_selector.setCurrentIndex(w.theme_selector.currentIndex())
    w._settings_theme_selector.blockSignals(False)
    project = w.registry.selected()
    w._settings_local_health.setText(w.connection_label.text())
    w._settings_kandev_status.setText(w.kandev_panel.status.text())
    w._settings_project_summary.setText(f'Project: {project.name}\n{project.path}' if project else 'No project selected')
    w._settings_providers.refresh()
    w._settings_dialog.show(); w._settings_dialog.raise_()
