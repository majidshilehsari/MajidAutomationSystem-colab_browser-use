"""Minimal Chrome DevTools Protocol client.

Standard library only. Chrome is started with ``--remote-debugging-port`` bound
to localhost, so this talks to a local port and never leaves the Colab runtime.

Only what the detector needs is implemented: list the page targets, evaluate an
expression in the page, read the result back as JSON.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import struct
from typing import Any, Dict, List, Optional, Tuple

CDP_PORT = 9222
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CdpError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP side: /json/list
# ---------------------------------------------------------------------------
def list_targets(port: int = CDP_PORT, timeout: float = 3.0) -> List[Dict[str, Any]]:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/json/list")
        response = conn.getresponse()
        raw = response.read()
        conn.close()
    except OSError as exc:
        raise CdpError("cannot reach the debugging port %d: %s" % (port, exc))
    if response.status != 200:
        raise CdpError("debugging port answered %d" % response.status)
    try:
        targets = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise CdpError("debugging port returned invalid JSON: %s" % exc)
    return [t for t in targets if isinstance(t, dict)]


def pick_page_target(port: int = CDP_PORT) -> Optional[Dict[str, Any]]:
    """Prefer a real page over devtools/service worker targets."""
    for wanted in ("page", "tab"):
        for target in list_targets(port):
            if target.get("type") == wanted and target.get("webSocketDebuggerUrl"):
                return target
    return None


# ---------------------------------------------------------------------------
# WebSocket framing (client side, RFC 6455)
# ---------------------------------------------------------------------------
def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise CdpError("connection closed while reading the frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def ws_connect(url: str, timeout: float = 5.0) -> socket.socket:
    if not url.startswith("ws://"):
        raise CdpError("only ws:// URLs are supported, got %r" % url)
    rest = url[len("ws://"):]
    host_port, _, path = rest.partition("/")
    path = "/" + path
    host, _, port_text = host_port.partition(":")
    port = int(port_text) if port_text else 80

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock = socket.create_connection((host, port), timeout=timeout)
    request = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host_port, key)
    )
    sock.sendall(request.encode("ascii"))

    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sock.recv(1024)
        if not chunk:
            raise CdpError("server closed during the WebSocket handshake")
        header += chunk
        if len(header) > 65536:
            raise CdpError("handshake headers are too large")
    head, _, leftover = header.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
    if " 101 " not in status_line:
        raise CdpError("WebSocket handshake rejected: %s" % status_line)

    expected = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    if expected.lower() not in head.decode("latin-1").lower():
        raise CdpError("Sec-WebSocket-Accept did not match the key we sent")

    sock.settimeout(timeout)
    if leftover:
        sock._automation_buffer = leftover  # type: ignore[attr-defined]
    return sock


def _read(sock: socket.socket, count: int) -> bytes:
    buffered = getattr(sock, "_automation_buffer", b"")
    if buffered:
        take, rest = buffered[:count], buffered[count:]
        sock._automation_buffer = rest  # type: ignore[attr-defined]
        if len(take) == count:
            return take
        return take + _recv_exact(sock, count - len(take))
    return _recv_exact(sock, count)


def ws_send(sock: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    mask_bit = 0x80
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += struct.pack(">H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", length)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def ws_recv(sock: socket.socket) -> Tuple[int, bytes]:
    """Return one complete message, joining continuation frames."""
    message = b""
    while True:
        first, second = _read(sock, 2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", _read(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _read(sock, 8))[0]
        if masked:  # servers must not mask, but stay tolerant
            mask = _read(sock, 4)
        payload = _read(sock, length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        if opcode == 0x8:  # close
            raise CdpError("server closed the WebSocket")
        if opcode == 0x9:  # ping -> pong
            ws_send(sock, payload, opcode=0xA)
            continue
        if opcode == 0xA:  # pong
            continue
        message += payload
        if fin:
            return opcode, message


def ws_close(sock: socket.socket) -> None:
    try:
        ws_send(sock, b"", opcode=0x8)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# High level helper
# ---------------------------------------------------------------------------
def evaluate(expression: str, port: int = CDP_PORT, timeout: float = 8.0) -> Dict[str, Any]:
    """Evaluate ``expression`` in the foreground page and return its JSON value."""
    target = pick_page_target(port)
    if target is None:
        raise CdpError("no page target is available on port %d" % port)
    sock = ws_connect(target["webSocketDebuggerUrl"], timeout=timeout)
    try:
        request = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }
        ws_send(sock, json.dumps(request).encode("utf-8"))
        while True:
            _, raw = ws_recv(sock)
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if message.get("id") != 1:
                continue
            if "error" in message:
                raise CdpError("CDP error: %s" % message["error"])
            result = message.get("result", {}).get("result", {})
            if result.get("subtype") == "error":
                raise CdpError("page raised: %s" % result.get("description"))
            return {"value": result.get("value"), "url": target.get("url"),
                    "title": target.get("title"), "targetId": target.get("id")}
    finally:
        ws_close(sock)
