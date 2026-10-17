"""Compact provider preferences and project-scoped, just-in-time permission."""
import platform

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ..molicoder.providers import AGENT_PROFILES


PROVIDERS = tuple(p.provider for p in AGENT_PROFILES if p.selectable)


def provider_name(provider):
    return 'OpenCode' if provider == 'opencode' else provider.title()


def installable_tools():
    from ..environment.manifests import load_manifests
    architecture = {'amd64': 'x86_64', 'x64': 'x86_64', 'x86_64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'aarch64'}.get(platform.machine().lower())
    return {m.tool_id for m in load_manifests()
            if m.system == platform.system() and m.architecture == architecture}


def provider_status(w, provider):
    from ..environment.provider_connection import cached_connection, connection_model
    checked = cached_connection(w.store, provider, connection_model(w, provider))
    if checked:
        labels = {'ready': 'Ready', 'sign_in_required': 'Sign-in required', 'access_blocked': 'Access blocked',
                  'test_failed': 'Connection failed', 'cancelled': 'Setup cancelled', 'not_installed': 'Not installed'}
        return labels[checked.state], checked.detail
    status = getattr(w, '_provider_statuses', {}).get(provider)
    if status:
        if not status.installed:
            return 'Not installed', status.detail
        if not status.auth_verified:
            return 'Installed · Sign-in not verified', status.detail
        if provider in {'opencode', 'kilo'} and status.authenticated and status.model in {'opencode/big-pickle', 'kilo/kilo-auto/free'}:
            return 'Free model listed · Test connection', status.detail
        return ('Signed in · Connection not tested' if status.authenticated else 'Sign-in required'), status.detail
    from ..orchestration.catalog import CapabilityCatalogService
    runtime = next((r for r in CapabilityCatalogService(w.store, w.client).cached().get('runtimes', [])
                    if r['tool_id'] == provider), None)
    if runtime:
        return ('Installed · Sign-in not verified' if runtime['installed'] else 'Not installed'), ''
    return 'Not checked', ''


def save_access(w, project, selected, automatic):
    """Read latest preferences so an open dialog cannot undo model edits."""
    preferences = w.store.get_project_preferences(project.id)
    trusted = (set(preferences.trusted_providers) - set(PROVIDERS)) | set(selected)
    w.store.set_project_preferences(project.id, provider=preferences.provider, strategy=preferences.strategy,
        trusted_providers=tuple(sorted(trusted)), model_overrides=preferences.model_overrides)
    w.store.set_setting(f'repository.external_fallback.{project.id}', str(automatic).lower())
    w.store.set_setting(f'ui.provider_setup.{project.id}', 'seen')
    w._refresh_build_readiness()
    w._refresh_route_identity()


def choose_project_access(w, project, *, needed=(), first_open=False, build=False, explanation='', valid=lambda: True):
    """One permission decision for a project, with a captured scope and owner guard."""
    dialog = QDialog(w)
    dialog.setWindowTitle('Choose providers for this project' if not needed else 'Another provider is needed')
    dialog.resize(640, 540)
    outer = QVBoxLayout(dialog)
    scroll = QScrollArea(); scroll.setWidgetResizable(True)
    page = QWidget(); layout = QVBoxLayout(page)
    scroll.setWidget(page); outer.addWidget(scroll)
    intro = QLabel(f'{project.name}\n{project.path}')
    intro.setWordWrap(True); layout.addWidget(intro)
    note = QLabel((explanation + '\n\n' if explanation else 'Allow the selected Build providers to continue.\n\n' if build else
        'The available local models could not complete this request. Select a provider to continue.\n\n' if needed else '') +
        'Selected providers may receive messages and relevant project files. Account charges may apply. '
        'File changes and commands keep their existing review steps.')
    note.setWordWrap(True); layout.addWidget(note)
    preferences = w.store.get_project_preferences(project.id)
    checks = {}
    for provider in PROVIDERS:
        state, detail = provider_status(w, provider)
        check = QCheckBox(f'{provider_name(provider)} · {state}')
        check.setObjectName('allow_' + provider)
        check.setChecked(provider in preferences.trusted_providers)
        check.setToolTip(detail)
        # On-demand selection only offers discovered candidates; setup can grant
        # access before installation so the user can finish setup in Settings.
        check.setEnabled(not needed or provider in needed or check.isChecked())
        checks[provider] = check; layout.addWidget(check)
    automatic = QCheckBox('Use selected providers automatically')
    automatic.setChecked(w.store.get_setting(f'repository.external_fallback.{project.id}', 'true') == 'true')
    automatic.setToolTip('Ask before using another provider. Turn off to keep chat requests local. Build still asks for its selected provider.')
    layout.addWidget(automatic)
    hint = QLabel('Downloads and sign-in are available in Settings → Models & providers.')
    hint.setWordWrap(True); layout.addWidget(hint)
    if not needed:
        setup = QPushButton('Install / sign in…')
        setup.clicked.connect(lambda: (dialog.reject(), w._provider_settings()))
        layout.addWidget(setup)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Allow and continue' if build else 'Continue request' if needed else 'Use selection')
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Cancel request' if needed else 'Later')
    if needed:
        def enabled():
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                (build or automatic.isChecked()) and
                (all(checks[p].isChecked() for p in needed) if build else any(checks[p].isChecked() for p in needed)))
        for check in checks.values(): check.toggled.connect(enabled)
        automatic.toggled.connect(enabled); enabled()
    buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
    layout.addStretch()
    outer.addWidget(buttons)
    if first_open:
        # A deferred choice is remembered; it remains available from Settings and
        # will be asked again when a request actually needs external inference.
        w.store.set_setting(f'ui.provider_setup.{project.id}', 'seen')
    def apply():
        if not valid(): return False
        save_access(w, project, [p for p, check in checks.items() if check.isChecked()], automatic.isChecked())
        panel = getattr(w, '_settings_providers', None)
        if panel: panel.refresh()
        return True
    if first_open:
        dialog.accepted.connect(apply)
        dialog.finished.connect(lambda: setattr(w, '_project_setup_dialog', None))
        w._project_setup_dialog = dialog
        dialog.open()
        return dialog
    return dialog.exec() == QDialog.DialogCode.Accepted and apply()


class ProviderPanel(QWidget):
    """A single detail editor, shared by every coding runtime."""
    def __init__(self, w):
        super().__init__()
        from .provider_connection import show_provider_connection
        self.w, self.project_id, self.loading = w, None, False
        body = QVBoxLayout(self); body.setContentsMargins(0, 0, 0, 0)
        self.scope = QLabel(); self.scope.setWordWrap(True); body.addWidget(self.scope)
        row = QHBoxLayout(); body.addLayout(row)
        self.access = QPushButton('Project access…'); row.addWidget(self.access)
        self.check = QPushButton('Refresh connections'); row.addWidget(self.check)
        self.access.clicked.connect(self.choose_access); self.check.clicked.connect(self.check_connections)
        self.providers = QListWidget()
        self.providers.setStyleSheet('QListWidget::item { padding: 4px 8px; margin: 0; }')
        self.providers.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.providers.setAccessibleName('Providers and connection status')
        for provider in PROVIDERS: self.providers.addItem(provider_name(provider))
        body.addWidget(self.providers)
        self.detail = QLabel(); self.detail.setWordWrap(True); body.addWidget(self.detail)
        actions = QHBoxLayout(); body.addLayout(actions)
        self.install = QPushButton('Download and install'); actions.addWidget(self.install)
        self.guide = QPushButton('Set up / test connection'); actions.addWidget(self.guide)
        self.install.clicked.connect(lambda: w._show_environment_doctor(install_tool=self.provider))
        self.guide.clicked.connect(lambda: show_provider_connection(w, self.provider))
        self.advanced = QPushButton('Model override (advanced)'); self.advanced.setCheckable(True); body.addWidget(self.advanced)
        self.model = QLineEdit(); self.model.setPlaceholderText('Automatic · provider default')
        self.model.setAccessibleName('Model override for selected provider'); body.addWidget(self.model)
        self.saved = QLabel('Changes are saved automatically.'); body.addWidget(self.saved)
        self.model.textChanged.connect(self.save_model)
        self.advanced.toggled.connect(self.model.setVisible)
        self.advanced.toggled.connect(self.saved.setVisible)
        self.providers.currentRowChanged.connect(self.render_detail)
        self.providers.setCurrentRow(0)

    @property
    def provider(self):
        return PROVIDERS[max(0, self.providers.currentRow())]

    def refresh(self):
        project = self.w.registry.selected()
        self.project_id = project.id if project else None
        self.scope.setText(f'Providers for {project.name}' if project else 'Choose a project to set provider access and model overrides.')
        self.access.setEnabled(project is not None)
        trusted = self.w.store.get_project_preferences(project.id).trusted_providers if project else ()
        for i, provider in enumerate(PROVIDERS):
            state, _ = provider_status(self.w, provider)
            item = self.providers.item(i)
            item.setText(f'{provider_name(provider)} · {state}' + (' · Allowed' if provider in trusted else ''))
            item.setToolTip(item.text())
        self.providers.setFixedHeight(sum(self.providers.sizeHintForRow(i) for i in range(len(PROVIDERS))) + 8)
        self.render_detail()

    def render_detail(self, *_):
        self.loading = True
        try:
            state, detail = provider_status(self.w, self.provider)
            self.detail.setText(detail)
            self.detail.setVisible(bool(detail))
            self.install.setVisible(self.provider in installable_tools() and state in {'Not installed', 'Not checked'})
            self.install.setEnabled(getattr(self.w, 'managed_install_worker', None) is None)
            self.guide.setText('Test connection' if state == 'Ready' else 'Set up / sign in')
            self.guide.show()
            project = self.w.registry.selected()
            valid = project is not None and project.id == self.project_id
            value = self.w.store.get_project_preferences(project.id).model_overrides.get(self.provider, '') if valid else ''
            self.model.setText(value); self.advanced.setChecked(bool(value))
            self.model.setVisible(bool(value)); self.saved.setVisible(bool(value))
            self.model.setEnabled(valid); self.advanced.setEnabled(valid)
            self.saved.setText('Changes are saved automatically.')
        finally:
            self.loading = False

    def save_model(self, value):
        if self.loading: return
        project = self.w.registry.selected()
        if project is None or project.id != self.project_id:
            self.refresh(); return
        preferences = self.w.store.get_project_preferences(project.id)
        models = dict(preferences.model_overrides)
        if value.strip(): models[self.provider] = value.strip()
        else: models.pop(self.provider, None)
        self.w.store.set_project_preferences(project.id, provider=preferences.provider, strategy=preferences.strategy,
            trusted_providers=preferences.trusted_providers, model_overrides=models)
        self.saved.setText('Saved · Applies to the next request or Build')
        self.w._refresh_route_identity()

    def choose_access(self):
        project = self.w.registry.selected()
        if project and project.id == self.project_id:
            choose_project_access(self.w, project, valid=lambda: self.w.current_project_id == project.id)

    def check_connections(self):
        from .settings_window import ProviderCheck
        if getattr(self.w, '_provider_check_worker', None): return
        worker = ProviderCheck(self.w); self.w._provider_check_worker = worker
        self.check.setEnabled(False); self.check.setText('Checking…')
        def ready(statuses):
            from ..environment.provider_connection import forget_connections
            self.w._provider_statuses = {s.provider: s for s in statuses}
            for status in statuses:
                if not status.installed or (status.auth_verified and not status.authenticated):
                    forget_connections(self.w.store, status.provider)
            from ..orchestration.catalog import CapabilityCatalogService
            catalog = CapabilityCatalogService(self.w.store, self.w.client)
            facts = catalog.cached()
            previous = {r['tool_id']: r for r in facts.get('runtimes', [])}
            for status in statuses:
                previous[status.provider] = {**previous.get(status.provider, {}),
                    'tool_id': status.provider, 'installed': status.installed, 'version': status.version}
            facts['runtimes'] = list(previous.values())
            import json
            self.w.store.set_setting(catalog.KEY, json.dumps(facts))
            self.refresh()
        def failed(error):
            self.detail.setText(error); self.detail.show()
        worker.ready.connect(ready); worker.failed.connect(failed)
        worker.finished.connect(lambda: (self.check.setEnabled(True), self.check.setText('Refresh connections'),
            setattr(self.w, '_provider_check_worker', None), worker.deleteLater()))
        worker.start()
