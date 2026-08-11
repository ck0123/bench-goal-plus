"""Ephemeral HTTP CONNECT proxy restricted to exact upstream targets."""

from __future__ import annotations

import atexit
import selectors
import socket
import socketserver
import threading
import urllib.parse
from collections.abc import Callable, Iterable
from typing import Any


Target = tuple[str, int]


def _parse_authority(authority: str) -> Target | None:
    try:
        parsed = urllib.parse.urlsplit(f"//{authority}")
        port = parsed.port
    except ValueError:
        return None
    if (
        not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname.rstrip(".").lower(), port


def _relay(left: socket.socket, right: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    try:
        selector.register(left, selectors.EVENT_READ, right)
        selector.register(right, selectors.EVENT_READ, left)
        while True:
            events = selector.select(timeout=1.0)
            for key, _ in events:
                source = key.fileobj
                destination = key.data
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    destination.sendall(data)
                except OSError:
                    return
    finally:
        selector.close()


class _ConnectProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: Target, allowed_targets: frozenset[Target]):
        self.allowed_targets = allowed_targets
        super().__init__(address, _ConnectProxyHandler)


class _ConnectProxyHandler(socketserver.BaseRequestHandler):
    server: _ConnectProxyServer

    def handle(self) -> None:
        self.request.settimeout(10.0)
        request = bytearray()
        while b"\r\n\r\n" not in request and len(request) < 65536:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            request.extend(chunk)
        if b"\r\n\r\n" not in request:
            self.request.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
            return
        try:
            request_line = bytes(request).split(b"\r\n", 1)[0].decode("ascii")
            method, authority, version = request_line.split(" ", 2)
        except (UnicodeDecodeError, ValueError):
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        target = _parse_authority(authority)
        if (
            method != "CONNECT"
            or version not in {"HTTP/1.0", "HTTP/1.1"}
            or target not in self.server.allowed_targets
        ):
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            upstream = socket.create_connection(target, timeout=10.0)
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        with upstream:
            self.request.settimeout(None)
            upstream.settimeout(None)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _relay(self.request, upstream)


def start_allowlisted_connect_proxy(
    *,
    listen_host: str,
    allowed_targets: Iterable[Target],
    name: str,
) -> tuple[dict[str, Any], Callable[[], None]]:
    normalized = frozenset(
        (host.rstrip(".").lower(), int(port)) for host, port in allowed_targets
    )
    if not normalized:
        raise ValueError("CONNECT proxy requires at least one allowed target")
    server = _ConnectProxyServer((listen_host, 0), normalized)
    listen_port = int(server.server_address[1])
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"{name}-connect-proxy",
        daemon=True,
    )
    thread.start()
    closed = False
    metadata = {
        "name": name,
        "listen_host": listen_host,
        "listen_port": listen_port,
        "allowed_targets": [
            {"host": host, "port": port} for host, port in sorted(normalized)
        ],
        "policy": "connect-allowlist",
        "request_content_logged": False,
        "closed": False,
    }

    def close_proxy() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        metadata["closed"] = True

    atexit.register(close_proxy)
    return metadata, close_proxy
