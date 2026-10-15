"""Opt-in loopback TLS transport for signed worker control messages.

Callers supply their own TLS certificate and decide when to start the server.
No listener is started by importing this module or launching MoliCoder.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..storage import ChatStore
from .leases import LeaseConflict, RemoteLeaseStore
from .messages import MAX_ENVELOPE_BYTES, RemoteMessageStore, canonical_bytes


PATH = "/v1/worker-message"
MAX_RESPONSE_BYTES = 4096


def create_loopback_server(store: ChatStore, certificate: Path, private_key: Path,
                           *, port: int = 0) -> ThreadingHTTPServer:
    """Prepare an opt-in TLS server bound only to IPv4 loopback."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(certificate), str(private_key))
    messages = RemoteMessageStore(store)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, _format: str, *_args) -> None:
            return  # never log message bodies, identities, or signature headers

        def _reply(self, status: int, result: dict) -> None:
            body = json.dumps(result, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_POST(self) -> None:
            if self.path != PATH:
                self._reply(404, {"error": "unknown route"})
                return
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/json":
                self._reply(400, {"error": "unsupported request encoding"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._reply(400, {"error": "missing request length"})
                return
            if length <= 0 or length > MAX_ENVELOPE_BYTES:
                self._reply(413, {"error": "request size is invalid"})
                return
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete worker message")
                envelope = json.loads(raw)
                receipt = messages.receive(envelope, self.headers.get("X-Worker-Signature", ""))
            except (UnicodeError, json.JSONDecodeError, ValueError, LeaseConflict):
                self._reply(401, {"error": "worker message was rejected"})
                return
            self._reply(200, {
                "message_id": receipt.message_id,
                "task_id": receipt.task_id,
                "epoch": receipt.epoch,
                "sequence": receipt.sequence,
                "received_at": receipt.received_at,
                "cancel_requested": (
                    envelope["kind"] == "heartbeat" and
                    RemoteLeaseStore(store).cancel_requested(receipt.task_id, receipt.epoch)
                ),
            })

    class LoopbackServer(ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self):
            connection, address = self.socket.accept()
            connection.settimeout(5)
            try:
                return context.wrap_socket(connection, server_side=True), address
            except Exception:
                connection.close()
                raise

    return LoopbackServer(("127.0.0.1", port), Handler)


def post_signed_message(port: int, envelope: dict, private_key: Ed25519PrivateKey,
                        certificate: Path, expected_fingerprint: str) -> dict:
    """Send once after validating TLS trust and the exact coordinator pin."""
    if not 0 < port < 65536 or not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("worker transport configuration is invalid")
    if len(expected_fingerprint) != 64 or any(char not in "0123456789abcdef" for char in expected_fingerprint):
        raise ValueError("coordinator certificate pin is invalid")
    body = canonical_bytes(envelope)
    signature = base64.b64encode(private_key.sign(body)).decode("ascii")
    context = ssl.create_default_context(cafile=str(certificate))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    connection = http.client.HTTPSConnection("127.0.0.1", port, timeout=8, context=context)
    try:
        connection.connect()
        peer = connection.sock.getpeercert(binary_form=True)
        if hashlib.sha256(peer).hexdigest() != expected_fingerprint:
            raise LeaseConflict("coordinator certificate pin differs")
        connection.request("POST", PATH, body=body, headers={
            "Content-Type": "application/json",
            "X-Worker-Signature": signature,
        })
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("coordinator response exceeds size limit")
        result = json.loads(data)
        if response.status != 200:
            raise LeaseConflict("coordinator rejected the worker message")
        if (result.get("message_id"), result.get("task_id"), result.get("epoch"), result.get("sequence")) != (
                envelope["message_id"], envelope["task_id"], envelope["epoch"], envelope["sequence"]):
            raise LeaseConflict("coordinator receipt does not match the request")
        return result
    finally:
        connection.close()
