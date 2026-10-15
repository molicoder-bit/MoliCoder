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
            if not list(Path(temporary).glob('smoke.sqlite3.pre-v15-*.bak')):
                raise RuntimeError('Legacy schema backup was not created')
            doctor_report(discover_environment(), load_manifests())
            window.close()
        return 0
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
