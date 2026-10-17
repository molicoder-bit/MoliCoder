"""One-click provider setup with live confirmation and owned login processes."""
from PySide6.QtCore import QThread, Signal, Qt, QUrl, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout
import re
from urllib.parse import urlsplit

from ..environment.provider_connection import ProviderConnection, connection_model, result, save_connection
from ..molicoder.providers import agent_profile, ProviderStatus


class ConnectionWorker(QThread):
    progress = Signal(str)
    checked = Signal(object)
    login_started = Signal()

    def __init__(self, provider, model, mode, parent):
        super().__init__(parent)
        self.service = ProviderConnection(provider, model, progress=self.progress.emit)
        self.service.on_login = self.login_started.emit
        self.mode = mode
        self.user_cancelled = False

    def cancel(self):
        self.user_cancelled = True
        self.service.cancel()

    def run(self):
        try:
            checked = (self.service.login(terminal=True) if self.mode == 'terminal' else
                       self.service.login(device=True) if self.mode == 'device' else
                       self.service.check() if self.mode == 'test' else self.service.connect())
        except InterruptedError:
            checked = result(self.service.provider, self.service.model, 'cancelled', 'Setup cancelled.')
        except Exception as exc:
            checked = result(self.service.provider, self.service.model, 'test_failed', str(exc), self.service.runtime.discover_binary())
        self.checked.emit(checked)


def show_provider_connection(w, provider):
    existing = getattr(w, '_provider_connection_dialog', None)
    if existing:
        existing.show(); existing.raise_(); return existing
    from .provider_setup import provider_name, installable_tools
    model = connection_model(w, provider)
    dialog = QDialog(w); dialog.setWindowTitle(provider_name(provider) + ' setup and connection')
    dialog.resize(650, 460)
    layout = QVBoxLayout(dialog)
    title = QLabel(f'{provider_name(provider)} · {model or "Provider default"}'); layout.addWidget(title)
    note = QLabel('Checks your existing sign-in, then sends a small test using this model. Provider charges may apply. '
        'If sign-in is needed, follow the browser prompt or the PowerShell window. Connection is checked again afterward.')
    note.setWordWrap(True); layout.addWidget(note)
    status = QLabel('Checking…'); status.setWordWrap(True); status.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(status)
    output = QPlainTextEdit(); output.setReadOnly(True); output.setAccessibleName('Sign-in instructions and progress'); layout.addWidget(output)
    actions = QHBoxLayout(); layout.addLayout(actions)
    retry = QPushButton('Retry connection'); actions.addWidget(retry)
    terminal = QPushButton('Sign in with PowerShell'); actions.addWidget(terminal)
    install = QPushButton('Download and install'); actions.addWidget(install); install.hide()
    bottom = QHBoxLayout(); layout.addLayout(bottom)
    browser = QPushButton('Open sign-in page'); bottom.addWidget(browser); browser.hide()
    device = QPushButton('Use device code'); bottom.addWidget(device); device.setVisible(provider in {'codex', 'copilot'})
    guide = QPushButton('Official help'); bottom.addWidget(guide)
    close = QPushButton('Cancel'); bottom.addWidget(close)
    guide.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(agent_profile(provider).auth_guide)))
    install.clicked.connect(lambda: w._show_environment_doctor(install_tool=provider))
    dialog.worker = None
    dialog.pending_mode = None
    dialog.sign_in_url = ''
    browser.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(dialog.sign_in_url)))
    w._provider_connection_dialog = dialog
    if not hasattr(w, '_provider_connection_workers'): w._provider_connection_workers = set()

    def begin(mode='connect'):
        if dialog.worker or getattr(w, '_provider_connection_dialog', None) is not dialog: return
        output.clear(); browser.hide(); dialog.sign_in_url = ''
        retry.setEnabled(False); terminal.setEnabled(False); device.setEnabled(False); install.hide(); close.setText('Cancel')
        status.setText('Checking existing sign-in and model access…')
        worker = ConnectionWorker(provider, model, mode, w)
        dialog.worker = worker; w._provider_connection_workers.add(worker)
        def progress(text):
            if dialog.worker is not worker or worker.user_cancelled: return
            output.appendPlainText(text.rstrip())
            for url in re.findall(r'https://[^\s<>"\x1b]+', text):
                try: parsed = urlsplit(url)
                except ValueError: continue
                allowed = {'auth.openai.com', 'chatgpt.com'} if provider == 'codex' else {'github.com'} if provider == 'copilot' else set()
                if parsed.hostname in allowed and not parsed.username and not parsed.password:
                    dialog.sign_in_url = url; browser.show()
            # Keep device codes and authorization links in memory only.
            if output.document().blockCount() > 80:
                output.setPlainText('\n'.join(output.toPlainText().splitlines()[-60:]))
        def checked(value):
            if dialog.worker is not worker or worker.user_cancelled: return
            save_connection(w.store, value)
            statuses = getattr(w, '_provider_statuses', {})
            previous = statuses.get(provider)
            statuses[provider] = ProviderStatus(provider, value.state != 'not_installed', value.state == 'ready',
                previous.version if previous else '', value.model, value.detail, value.binary,
                value.state == 'sign_in_required')
            w._provider_statuses = statuses
            labels = {'ready': 'Ready · Model connection verified', 'sign_in_required': 'Sign-in still required',
                'not_installed': 'Provider is not installed', 'access_blocked': 'Account access is blocked',
                'test_failed': 'Connection test failed', 'cancelled': 'Setup cancelled'}
            status.setText(labels[value.state] + '\n' + value.detail)
            if value.state == 'ready':
                output.clear(); browser.hide(); dialog.sign_in_url = ''
            install.setVisible(value.state == 'not_installed' and provider in installable_tools())
            if hasattr(w, '_settings_providers'): w._settings_providers.refresh()
            dialog.last_result = value
        def finished():
            w._provider_connection_workers.discard(worker)
            if dialog.worker is worker:
                dialog.worker = None; retry.setEnabled(True); terminal.setEnabled(True); device.setEnabled(True); close.setText('Close')
                pending, dialog.pending_mode = dialog.pending_mode, None
                if pending and getattr(w, '_provider_connection_dialog', None) is dialog:
                    QTimer.singleShot(0, lambda: begin(pending))
            worker.deleteLater()
        worker.progress.connect(progress); worker.checked.connect(checked); worker.finished.connect(finished)
        worker.login_started.connect(lambda: (terminal.setEnabled(True), device.setEnabled(True))
            if dialog.worker is worker and not worker.user_cancelled else None)
        worker.start()
    def switch_login(mode):
        if dialog.worker:
            dialog.pending_mode = mode
            terminal.setEnabled(False); device.setEnabled(False)
            status.setText('Switching sign-in method…')
            dialog.worker.cancel()
        else: begin(mode)
    def dismiss():
        worker, dialog.worker = dialog.worker, None
        dialog.pending_mode = None
        if worker: worker.cancel()
        if getattr(w, '_provider_connection_dialog', None) is dialog:
            w._provider_connection_dialog = None
        output.clear()
    dialog.finished.connect(dismiss)
    retry.clicked.connect(lambda: begin('test')); terminal.clicked.connect(lambda: switch_login('terminal'))
    device.clicked.connect(lambda: switch_login('device'))
    close.clicked.connect(dialog.reject)
    dialog.show(); begin()
    return dialog
