from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .config import APP_NAME
from .ui.main_window import MainWindow
from .ui.styles import APP_STYLESHEET


def main() -> int:
    if "--package-health-check" in sys.argv:
        return 0
    smoke_check = "--package-smoke-check" in sys.argv
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("molicoder-bit")
    app.setStyle("Fusion")
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI Variable", 10))
    app.setStyleSheet(APP_STYLESHEET)
    if smoke_check:
        from .environment.discovery import discover_environment
        from .environment.doctor import doctor_report
        from .environment.manifests import load_manifests
        from .storage import ChatStore

        with tempfile.TemporaryDirectory(prefix="molicoder-smoke-") as temporary:
            window = MainWindow(
                store=ChatStore(Path(temporary) / "smoke.sqlite3"),
                start_ollama_check=False,
            )
            window.show()
            app.processEvents()
            if not window.composer or not window.build_request:
                raise RuntimeError("Chat or Build view failed to initialize")
            doctor_report(discover_environment(), load_manifests())
            window.close()
        return 0
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
