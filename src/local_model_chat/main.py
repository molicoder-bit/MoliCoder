from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from .config import APP_NAME
from .ui.main_window import MainWindow
from .ui.styles import APP_STYLESHEET


def main() -> int:
    if '--provider-connection' in sys.argv:
        from .environment.provider_connection import main as connection_main
        return connection_main(sys.argv[sys.argv.index('--provider-connection') + 1:])
    if '--request' in sys.argv:
        from .orchestration.cli import main as request_main
        return request_main(sys.argv[sys.argv.index('--request') + 1:])
    if '--package-live-file-check' in sys.argv:
        import json
        from .package_acceptance import verify_file_workflow
        report_path = Path(sys.argv[sys.argv.index('--package-live-file-check') + 1])
        try:
            with tempfile.TemporaryDirectory(prefix='molicoder-live-acceptance-') as directory:
                report = verify_file_workflow(Path(directory))
        except Exception as exc:
            report = {'status': 'failed', 'error': str(exc)}
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        return 0 if report['status'] == 'passed' else 1
    if "--package-health-check" in sys.argv:
        return 0
    smoke_check = "--package-smoke-check" in sys.argv
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("molicoder-bit.MoliCoder")
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "MoliCoder.ico")))
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("molicoder-bit")
    app.setStyle("Fusion")
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI Variable", 10))
    app.setStyleSheet(APP_STYLESHEET)
    if '--package-provider-smoke-check' in sys.argv:
        from .ui.provider_smoke import provider_smoke
        report = Path(sys.argv[sys.argv.index('--package-provider-smoke-check') + 1])
        return provider_smoke(app, report)
    if "--package-board-smoke-check" in sys.argv:
        from .ui.browser_smoke import browser_smoke
        return browser_smoke(app)
    if smoke_check:
        from .environment.discovery import discover_environment
        from .environment.doctor import doctor_report
        from .environment.manifests import load_manifests
        from .storage import ChatStore

        with tempfile.TemporaryDirectory(prefix="molicoder-smoke-") as temporary:
            import sqlite3
            from contextlib import closing
            from .domain import Message
            database = Path(temporary) / 'smoke.sqlite3'
            legacy = ChatStore(database)
            conversation = legacy.create_conversation('legacy-fixture-model')
            legacy.add_message(Message(None, conversation.id, 'user', 'Preserve this fixture history'))
            project = legacy.upsert_project('Legacy fixture', temporary, 'python')
            run_id = legacy.create_run(project.id, 'Interrupted fixture', 'HYBRID', provider='codex')
            task_id = legacy.create_run_task(run_id, 'fixture', 'Fixture', 'Fixture', 'low')
            legacy.update_run_task(task_id, 'running', 0)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute('DROP TABLE message_receipts')
                connection.execute('DROP TABLE local_action_results')
                connection.execute('PRAGMA user_version=14')
                connection.commit()
            upgraded = ChatStore(database)
            window = MainWindow(
                store=upgraded,
                start_ollama_check=False,
            )
            window.show()
            app.processEvents()
            if not window.composer or not window.build_request:
                raise RuntimeError("Chat or Build view failed to initialize")
            if upgraded.list_messages(conversation.id)[0].content != 'Preserve this fixture history':
                raise RuntimeError('Legacy chat history did not survive migration')
            if upgraded.get_run(run_id)['status'] != 'needs_attention':
                raise RuntimeError('Interrupted fixture was not reconciled')
            if not list(Path(temporary).glob(f'smoke.sqlite3.pre-v{ChatStore.SCHEMA_VERSION}-*.bak')):
                raise RuntimeError('Legacy schema backup was not created')
            from .ui.chat_smoke import verify_local_chat
            verify_local_chat(window, project.id, conversation.id, temporary, app)
            doctor_report(discover_environment(), load_manifests())
            window.close()
        return 0
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
