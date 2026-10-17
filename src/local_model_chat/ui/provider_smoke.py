"""Explicit CLI qualification of the frozen package's actual setup button."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time

from PySide6.QtCore import QTimer

from ..environment.provider_connection import ProviderConnection
from ..storage import ChatStore
from .main_window import MainWindow
from .provider_setup import PROVIDERS, provider_status


def provider_smoke(app, report_path):
    report = {'status': 'failed'}
    original = ProviderConnection.connect
    # Only this explicit package test suppresses interactive login. The model
    # request and strict nonce verification remain real; an auth failure fails
    # the gate instead of opening a browser or terminal during headless tests.
    ProviderConnection.connect = lambda service, **kwargs: service.check()
    try:
        with tempfile.TemporaryDirectory(prefix='molicoder-provider-ui-') as directory:
            store = ChatStore(Path(directory) / 'profile.db')
            project = store.upsert_project('Package setup check', directory, 'generic')
            window = MainWindow(store, start_ollama_check=False)
            window._load_project_list(select_id=project.id)
            window._provider_settings()
            panel = window._settings_providers
            panel.providers.setCurrentRow(PROVIDERS.index('opencode'))
            panel.guide.click()
            dialog = window._provider_connection_dialog
            timer = QTimer(window); started = time.monotonic(); timed_out = False
            def observe():
                nonlocal timed_out
                if dialog.worker:
                    if not timed_out and time.monotonic() - started > 130:
                        timed_out = True; dialog.worker.cancel()
                    return
                timer.stop()
                checked = getattr(dialog, 'last_result', None)
                trusted = store.get_project_preferences(project.id).trusted_providers
                report.update(status='passed' if checked and checked.state == 'ready' and not trusted and not timed_out else 'failed',
                    button_clicked=True, connection=asdict(checked) if checked else None,
                    list_status=provider_status(window, 'opencode')[0], project_trust_granted=bool(trusted), timed_out=timed_out)
                dialog.reject(); window._settings_dialog.close(); window.close(); app.quit()
            timer.timeout.connect(observe); timer.start(30)
            app.exec()
    except Exception as exc:
        report.update(error=str(exc))
    finally:
        ProviderConnection.connect = original
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['status'] == 'passed' else 1
