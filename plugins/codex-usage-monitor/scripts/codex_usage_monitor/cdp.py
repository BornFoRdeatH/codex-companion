from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import queue
import socket
import ssl
import struct
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Any


class CdpError(RuntimeError):
    pass


def discover_targets(port: int, timeout: float = 0.5) -> list[dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/json/list")
        response = connection.getresponse()
        if response.status != 200:
            raise CdpError(f"CDP discovery returned HTTP {response.status}")
        value = json.loads(response.read().decode("utf-8"))
        return value if isinstance(value, list) else []
    finally:
        connection.close()


@dataclass
class _Pending:
    values: queue.Queue[dict[str, Any]]


class CdpConnection:
    """Small RFC6455/CDP client with no third-party dependencies."""

    def __init__(self, websocket_url: str, timeout: float = 2.0):
        parsed = urllib.parse.urlparse(websocket_url)
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise CdpError("Refusing non-loopback CDP endpoint")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        sock = socket.create_connection((parsed.hostname, port), timeout=timeout)
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        headers = _read_headers(sock)
        if not headers.startswith("HTTP/1.1 101"):
            sock.close()
            raise CdpError("CDP WebSocket handshake failed")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if f"sec-websocket-accept: {expected}".lower() not in headers.lower():
            sock.close()
            raise CdpError("CDP WebSocket handshake validation failed")
        self.sock = sock
        self.sock.settimeout(None)
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        threading.Thread(target=self._reader, name="codex-usage-cdp", daemon=True).start()

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 2.0) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            pending = _Pending(queue.Queue(maxsize=1))
            self._pending[request_id] = pending
            self._send_json({"id": request_id, "method": method, "params": params or {}})
        try:
            response = pending.values.get(timeout=timeout)
        except queue.Empty as exc:
            self._pending.pop(request_id, None)
            raise CdpError(f"CDP timeout: {method}") from exc
        if "error" in response:
            raise CdpError(f"CDP {method}: {response['error']}")
        return response.get("result")

    def next_event(self, timeout: float = 0.0) -> dict[str, Any] | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._send_frame(b"", 0x8)
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _send_json(self, value: dict[str, Any]) -> None:
        self._send_frame(json.dumps(value, separators=(",", ":")).encode("utf-8"), 0x1)

    def _send_frame(self, payload: bytes, opcode: int) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        header.extend(bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))
        self.sock.sendall(header)

    def _reader(self) -> None:
        fragments = bytearray()
        fragment_opcode = 0
        try:
            while not self._closed.is_set():
                first, second = _recv_exact(self.sock, 2)
                fin, opcode, masked, length = bool(first & 0x80), first & 0x0F, bool(second & 0x80), second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", _recv_exact(self.sock, 2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", _recv_exact(self.sock, 8))[0]
                mask = _recv_exact(self.sock, 4) if masked else b""
                payload = _recv_exact(self.sock, length)
                if masked:
                    payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._send_frame(payload, 0xA)
                    continue
                if opcode in {0x1, 0x2}:
                    fragments = bytearray(payload)
                    fragment_opcode = opcode
                elif opcode == 0x0:
                    fragments.extend(payload)
                if fin and fragment_opcode == 0x1:
                    self._dispatch(json.loads(fragments.decode("utf-8")))
                    fragments = bytearray()
                    fragment_opcode = 0
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            self._closed.set()

    def _dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if isinstance(request_id, int):
            pending = self._pending.pop(request_id, None)
            if pending:
                pending.values.put(message)
                return
        self._events.put(message)


def _read_headers(sock: socket.socket) -> str:
    value = bytearray()
    while b"\r\n\r\n" not in value:
        value.extend(sock.recv(4096))
        if len(value) > 65536:
            raise CdpError("Oversized WebSocket response headers")
    return value.decode("latin-1")


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    value = bytearray()
    while len(value) < length:
        chunk = sock.recv(length - len(value))
        if not chunk:
            raise OSError("WebSocket closed")
        value.extend(chunk)
    return bytes(value)
