"""Exercise the packaged WebEngine helper against a harmless loopback fixture."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from PySide6.QtCore import QEventLoop, QTimer, QUrl


def browser_smoke(app):
    from PySide6.QtWebEngineWidgets import QWebEngineView
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
            self.wfile.write(b'<html><head><title>MoliCoder board smoke</title></head><body>Embedded board renderer ready</body></html>')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    Thread(target=server.serve_forever,daemon=True).start()
    view = QWebEngineView();loop = QEventLoop();result = [False]
    def loaded(ok):
        if not ok: loop.quit(); return
        view.page().runJavaScript('document.body.innerText', lambda text: (result.__setitem__(0, 'renderer ready' in (text or '')),loop.quit()))
    view.loadFinished.connect(loaded)
    timer=QTimer();timer.setSingleShot(True);timer.timeout.connect(loop.quit);timer.start(15000)
    view.setUrl(QUrl(f'http://127.0.0.1:{server.server_port}/'));view.show();loop.exec()
    timer.stop();view.close();view.deleteLater();app.processEvents();server.shutdown();server.server_close()
    return 0 if result[0] else 1
