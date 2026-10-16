"""Persistent, navigable settings with everyday actions first."""
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QVBoxLayout, QListWidget,
    QStackedWidget, QWidget, QLabel, QPushButton, QCheckBox, QComboBox, QScrollArea, QLineEdit, QFrame, QSpinBox)
from PySide6.QtCore import QThread, Signal


class ProviderCheck(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            from ..molicoder.providers import probe_all_providers
            self.ready.emit(probe_all_providers())
        except Exception as exc:
            self.failed.emit(str(exc))


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
            'Choose the model for your next Chat reply. External agents and API connections have separate supported uses.',
            'Choose a folder for repository work and review how Build runs.',
            'Review measured usage and set project limits. Subscription agent usage may be unavailable.',
            'Optional task board and agent ownership. Choose Kandev deliberately for each Build.',
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
                body.addWidget(QLabel('Local Chat model · App/next reply'))
                body.addWidget(w.model_combo); w.model_combo.show()
                detail = QLabel('Changing a model can open a new chat. Previous replies keep their recorded origin.'); detail.setWordWrap(True); body.addWidget(detail)
                action('Install or inspect local models', w._show_environment_doctor)
                action('Connect an account or configure Build models', w._provider_settings)
                health = QLabel(w.connection_label.text()); body.addWidget(health)
                w._settings_local_health = health
                provider_labels = {}
                model_fields = {}
                scope_label = QLabel('Project model overrides · Select a project first')
                scope_label.setWordWrap(True); body.addWidget(scope_label)
                for provider in ('codex', 'claude', 'copilot'):
                    card = QFrame(); card.setObjectName('metricCard'); card_body = QVBoxLayout(card)
                    heading = QLabel(f'{provider.title()} · External agent · Build only')
                    card_body.addWidget(heading)
                    readiness = QLabel('Not checked · Check connection to verify installation and sign-in')
                    readiness.setWordWrap(True); card_body.addWidget(readiness); provider_labels[provider] = readiness
                    choice = QComboBox(); choice.addItems(['Provider default · model may not be reported', 'Request a specific model'])
                    model = QLineEdit(); model.setPlaceholderText('Exact model ID from your provider')
                    choice.currentIndexChanged.connect(lambda index, field=model: field.setEnabled(index == 1))
                    card_body.addWidget(choice); card_body.addWidget(model)
                    save = QPushButton('Save model for this project'); card_body.addWidget(save)
                    def save_model(*_, name=provider, field=model, picker=choice, feedback=readiness):
                        project = w.registry.selected()
                        if project is None or project.id != getattr(w, '_settings_scope_id', None):
                            feedback.setText('Project changed. Reopen Settings before saving this project setting.'); return
                        value = field.text().strip() if picker.currentIndex() == 1 else ''
                        if picker.currentIndex() == 1 and not value:
                            feedback.setText('Enter a model ID or choose Provider default.'); field.setFocus(); return
                        preferences = w.store.get_project_preferences(project.id)
                        overrides = dict(preferences.model_overrides); overrides[name] = value
                        w.store.set_project_preferences(project.id, provider=preferences.provider, strategy=preferences.strategy,
                                                        trusted_providers=preferences.trusted_providers, model_overrides=overrides)
                        feedback.setText(f'Saved for {project.name} · Applies to the next Build call')
                        w._refresh_route_identity()
                    save.clicked.connect(save_model)
                    guide = QPushButton('Open official sign-in instructions')
                    def open_guide(*_, name=provider):
                        from PySide6.QtCore import QUrl
                        from PySide6.QtGui import QDesktopServices
                        from ..molicoder.providers import agent_profile
                        QDesktopServices.openUrl(QUrl(agent_profile(name).auth_guide))
                    guide.clicked.connect(open_guide); card_body.addWidget(guide)
                    model_fields[provider] = (choice, model, save)
                    body.addWidget(card)
                check_button = QPushButton('Check agent connections · no model request')
                body.addWidget(check_button)
                def check_connections():
                    check_button.setEnabled(False); check_button.setText('Checking installation and sign-in…')
                    worker = ProviderCheck(dialog); w._provider_check_worker = worker
                    def show_status(statuses):
                        for status in statuses:
                            if status.provider not in provider_labels:
                                continue
                            state = ('Not installed' if not status.installed else
                                     'Sign-in not verified' if not status.auth_verified else
                                     'Ready' if status.authenticated else 'Sign-in required')
                            provider_labels[status.provider].setText(f'{state}\n{status.detail}')
                    worker.ready.connect(show_status)
                    worker.failed.connect(lambda error: check_button.setToolTip(error))
                    worker.finished.connect(lambda: (check_button.setEnabled(True), check_button.setText('Check agent connections · no model request'), setattr(w, '_provider_check_worker', None), worker.deleteLater()))
                    worker.start()
                check_button.clicked.connect(check_connections)
                action('Add custom API connection', w._add_custom_provider)
                action('Add API connection from a preset', w._add_api_preset)
                body.addWidget(QLabel('API connections support capped tests; they do not change the local Chat route.'))
                from ..environment.security import platform_security
                security = platform_security()
                security_note = QLabel(security.reason); security_note.setWordWrap(True); body.addWidget(security_note)
                for account in w.inference_registry.list_account_statuses():
                    account_label = QLabel(f'{account.provider_id} · {account.auth_status} · API tests'); account_label.setWordWrap(True); body.addWidget(account_label)
                w._settings_model_fields, w._settings_scope_label = model_fields, scope_label
                body.addWidget(QLabel('Advanced generation controls and context estimates are in Performance.'))
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
    w._settings_scope_id = project.id if project else None
    w._settings_local_health.setText(w.connection_label.text())
    w._settings_kandev_status.setText(w.kandev_panel.status.text())
    w._settings_project_summary.setText(f'Project: {project.name}\n{project.path}' if project else 'No project selected')
    w._settings_scope_label.setText(f'Project model overrides: {project.name}\n{project.path}' if project else 'No project · Choose a project before saving a Build model')
    preferences = w.store.get_project_preferences(project.id) if project else None
    for provider, (choice, field, save) in w._settings_model_fields.items():
        value = preferences.model_overrides.get(provider, '') if preferences else ''
        field.setText(value); choice.setCurrentIndex(1 if value else 0)
        choice.setEnabled(project is not None); field.setEnabled(project is not None and bool(value)); save.setEnabled(project is not None)
    w._settings_dialog.show(); w._settings_dialog.raise_()
