from __future__ import annotations

import json
from collections.abc import Mapping
from uuid import uuid4

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

from .rest import KandevTransportError


def call_kandev_socket(endpoint: str, token: str, action: str, payload: Mapping[str, object]) -> Mapping[str, object]:
    """One bounded v0.97.0 WebSocket request; never persist a PAT in the URL."""
    if QCoreApplication.instance() is None:
        raise KandevTransportError("Kandev WebSocket requires the desktop event loop")
    base = QUrl(endpoint)
    if base.scheme() != "http" or base.host() not in {"127.0.0.1", "::1"} or base.port() < 1:
        raise KandevTransportError("Kandev WebSocket endpoint is invalid")
    url = QUrl()
    url.setScheme("ws")
    url.setHost(base.host())
    url.setPort(base.port())
    url.setPath("/ws")
    request = QNetworkRequest(url)
    if token:
        request.setRawHeader(b"Authorization", f"Bearer {token}".encode("utf-8"))
    call_id = uuid4().hex
    outgoing = json.dumps({
        "id": call_id, "type": "request", "action": action, "payload": dict(payload),
    }, separators=(",", ":"))
    socket = QWebSocket()
    socket.setMaxAllowedIncomingMessageSize(1_000_000)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    result: dict[str, object] = {}
    fault = "Kandev WebSocket timed out"

    def connected() -> None:
        socket.sendTextMessage(outgoing)

    def received(message: str) -> None:
        nonlocal fault
        if len(message) > 1_000_000:
            fault = "Kandev WebSocket response exceeds size limit"
            loop.quit()
            return
        try:
            incoming = json.loads(message)
        except json.JSONDecodeError:
            fault = "Kandev WebSocket returned invalid JSON"
            loop.quit()
            return
        if not isinstance(incoming, dict) or incoming.get("id") != call_id:
            return
        if incoming.get("action") != action:
            fault = "Kandev WebSocket response action differs"
        elif incoming.get("type") != "response" or not isinstance(incoming.get("payload"), dict):
            fault = "Kandev WebSocket action failed"
        else:
            result.update(incoming["payload"])
        loop.quit()

    socket.connected.connect(connected)
    socket.textMessageReceived.connect(received)
    socket.errorOccurred.connect(lambda _error: loop.quit())
    timer.timeout.connect(loop.quit)
    timer.start(5_000)
    socket.open(request)
    try:
        loop.exec()
    finally:
        timer.stop()
        socket.abort()
        socket.deleteLater()
    if not result:
        raise KandevTransportError(fault)
    return result
