"""Lazy real Kandev board and local service/connection controls."""
import json
from PySide6.QtCore import QThread, Signal, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QLineEdit, QDialog, QFormLayout, QComboBox, QDialogButtonBox, QMessageBox
from ..kandev.service import KandevServiceController, get_json
from ..kandev.ui_config import kandev_board_url


class Operation(QThread):
    done = Signal(object)
    error = Signal(str)
    def __init__(self, fn, parent):
        super().__init__(parent); self.fn = fn
    def run(self):
        try: self.done.emit(self.fn())
        except Exception as exc: self.error.emit(str(exc))


class KandevPanel(QWidget):
    def __init__(self, window):
        super().__init__(); self.window = window; self.worker = None; self.browser = None
        self.service = KandevServiceController()
        layout = QVBoxLayout(self)
        row = QGridLayout()
        for index, (text, callback) in enumerate([('Connect / reload', self.connect_board), ('Configure', self.configure), ('Install', window._show_environment_doctor),
                ('Start managed', self.start_service), ('Stop managed', self.stop_service), ('Open in browser', self.external)]):
            button = QPushButton(text); button.clicked.connect(callback); row.addWidget(button, index // 3, index % 3)
        layout.addLayout(row)
        self.status = QLabel('Kandev is optional. Connect a local service or install it in Settings → Environment.')
        self.status.setWordWrap(True); layout.addWidget(self.status)
        note = QLabel('Kandev owns actions in this board. Native agent token use and cost may be unknown. Managed service runs until stopped.')
        note.setWordWrap(True); note.setObjectName('muted'); layout.addWidget(note)
        self.body = QVBoxLayout(); layout.addLayout(self.body, 1)

    def origin(self):
        saved = self.window.store.get_setting(f'kandev.project.{self.window.current_project_id}', '')
        profile = json.loads(saved) if saved else {}
        return kandev_board_url({'KANDEV_ENDPOINT': profile['endpoint']}) if profile else kandev_board_url()

    def operation(self, fn, done):
        if self.worker is not None: return
        self.status.setText('Working…')
        self.worker = Operation(fn, self)
        worker = self.worker
        worker.done.connect(done); worker.error.connect(lambda message: self.status.setText('Needs attention · ' + message))
        worker.finished.connect(lambda: (setattr(self, 'worker', None), worker.deleteLater()))
        worker.start()

    def connect_board(self):
        try: origin = self.origin()
        except ValueError as exc: self.status.setText(str(exc)); return
        def loaded(health):
            version = str(health.get('version', '')).lstrip('v')
            if version != '0.97.0':
                self.status.setText(f'Unsupported service version: {version}. Use the pinned 0.97.0 service.'); return
            self.status.setText('Connected · ' + origin)
            if self.browser is not None and getattr(self, '_browser_origin', '') != origin:
                self.body.removeWidget(self.browser); self.browser.deleteLater(); self.browser = None
            self._browser_origin = origin
            if self.browser is None:
                from PySide6.QtWebEngineWidgets import QWebEngineView
                from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
                class LocalPage(QWebEnginePage):
                    def acceptNavigationRequest(page, url, navigation, main_frame):
                        if url.toString() == 'about:blank': return True
                        permitted = url.scheme() == 'http' and url.host() == QUrl(origin).host() and url.port() == QUrl(origin).port()
                        if not permitted and main_frame and navigation == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in {'https', 'http'}:
                            QDesktopServices.openUrl(url)
                        return permitted
                self.browser = QWebEngineView()
                self.profile = QWebEngineProfile(self.browser)  # separate ephemeral local board profile
                self.page = LocalPage(self.profile, self.browser)
                self.browser.setPage(self.page)
                self.page.newWindowRequested.connect(lambda request: QDesktopServices.openUrl(request.requestedUrl()) if request.isUserInitiated() and request.requestedUrl().scheme() in {'https', 'http'} else None)
                self.page.permissionRequested.connect(lambda permission: permission.deny())
                self.page.renderProcessTerminated.connect(lambda *_: self.status.setText('Board renderer stopped. Reconnect or open in browser.'))
                self.browser.loadFinished.connect(lambda ok: self.status.setText('Connected · ' + origin if ok else 'Board could not load. Reconnect or open in browser.'))
                self.body.addWidget(self.browser)
            self.browser.setUrl(QUrl(origin))
        self.operation(lambda: get_json(origin, '/health'), loaded)

    def external(self):
        QDesktopServices.openUrl(QUrl(self.origin()))

    def authorize_service(self, action, origin):
        from datetime import datetime, timedelta, timezone
        from ..authorization import AuthorizationPolicy, ActionRequest, ActionRisk
        self.window.store.record_action_authorization(project_id=None, action=action, scope=origin,
            risk='mutation', decision='approved', actor='local_user_kandev_service_button',
            expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
        AuthorizationPolicy(self.window.store).require(ActionRequest(None, action, origin, ActionRisk.MUTATION))

    def start_service(self):
        origin = self.origin()
        if QMessageBox.question(self, 'Start managed Kandev', f'Start pinned Kandev locally at {origin}?\nIts data is kept under MoliCoder. No agent is launched. The service stays running until stopped.') != QMessageBox.Yes: return
        self.authorize_service('kandev_service_start', origin)
        self.operation(lambda: self.service.start(origin), lambda _: self.status.setText('Service ready. Choose Connect / reload to open its board.'))

    def stop_service(self):
        origin = self.origin()
        if QMessageBox.question(self, 'Stop managed Kandev', 'Stop the service owned by MoliCoder? Active or unverified agent sessions prevent this action.') != QMessageBox.Yes: return
        self.authorize_service('kandev_service_stop', origin)
        self.operation(lambda: self.service.stop(origin), lambda result: self.status.setText(str(result)))

    def configure(self):
        project = self.window.registry.selected()
        if project is None:
            QMessageBox.information(self, 'Kandev', 'Select a project first.'); return
        dialog = QDialog(self); dialog.setWindowTitle('Connect project to Kandev'); dialog.resize(620, 420)
        form = QFormLayout(dialog)
        endpoint = QLineEdit(self.origin()); form.addRow('Local endpoint', endpoint)
        combos = {key: QComboBox() for key in ('workspace_id', 'repository_id', 'workflow_id', 'workflow_step_id', 'agent_profile_id')}
        for key, combo in combos.items(): form.addRow(key.replace('_id', '').replace('_', ' ').title(), combo)
        info = QLabel('Load choices from Kandev. Create missing resources in its board, then refresh.'); info.setWordWrap(True); form.addRow(info)
        refresh = QPushButton('Load / refresh choices'); form.addRow(refresh)
        def populate(combo, items):
            combo.clear()
            for item in items:
                combo.addItem(str(item.get('name') or item.get('title') or item.get('id')), str(item['id']))
        catalog = {}
        def fetch():
            try: origin = kandev_board_url({'KANDEV_ENDPOINT': endpoint.text().strip()})
            except ValueError as exc: info.setText(str(exc)); return
            def read_catalog():
                workspaces = get_json(origin, '/api/v1/workspaces').get('workspaces', [])
                agents = get_json(origin, '/api/v1/agents').get('agents', [])
                data = {'workspaces': workspaces, 'profiles': [p for agent in agents for p in agent.get('profiles', [])], 'workflows': {}, 'repositories': {}, 'steps': {}}
                for workspace in workspaces[:30]:
                    wid = workspace['id']
                    data['repositories'][wid] = get_json(origin, f'/api/v1/workspaces/{wid}/repositories').get('repositories', [])
                    workflows = get_json(origin, f'/api/v1/workspaces/{wid}/workflows').get('workflows', [])
                    data['workflows'][wid] = workflows
                    for workflow in workflows[:30]:
                        fid = workflow['id']
                        data['steps'][fid] = get_json(origin, f'/api/v1/workflows/{fid}').get('steps', [])
                return data
            def loaded(data):
                catalog.update(data)
                populate(combos['workspace_id'], data['workspaces'])
                populate(combos['agent_profile_id'], data['profiles'])
                load_workspace()
                info.setText('Choose the repository matching this project. Missing resources can be created in the board.')
            self.operation(read_catalog, loaded)
            info.setText('Loading local choices…')
        def load_workspace():
            wid = combos['workspace_id'].currentData()
            populate(combos['repository_id'], catalog.get('repositories', {}).get(wid, []))
            populate(combos['workflow_id'], catalog.get('workflows', {}).get(wid, []))
            load_steps()
        def load_steps():
            fid = combos['workflow_id'].currentData()
            populate(combos['workflow_step_id'], catalog.get('steps', {}).get(fid, []))
        refresh.clicked.connect(fetch); combos['workspace_id'].activated.connect(load_workspace); combos['workflow_id'].activated.connect(load_steps)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel); form.addRow(buttons)
        def save():
            try:
                if self.worker is not None: return
                profile = dict(endpoint=kandev_board_url({'KANDEV_ENDPOINT': endpoint.text().strip()}), version='0.97.0', **{key: combo.currentData() or '' for key, combo in combos.items()})
                from ..kandev.rest import KandevRestConfig
                KandevRestConfig(**profile)
                self.window.store.set_setting(f'kandev.project.{project.id}', json.dumps(profile)); dialog.accept()
                self.window._refresh_build_readiness()
            except Exception as exc: info.setText(str(exc))
        buttons.accepted.connect(save); buttons.rejected.connect(dialog.reject)
        def reject_while_busy():
            if self.worker is None: dialog.reject()
        buttons.rejected.disconnect()
        buttons.rejected.connect(reject_while_busy)
        dialog.setWindowFlag(Qt.WindowCloseButtonHint, False)
        dialog.exec()
