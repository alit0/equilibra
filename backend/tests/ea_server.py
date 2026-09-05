"""Tiny scripted EA HTTP server for transport-level tests.

Runs ``http.server`` in a daemon thread on an ephemeral port and lets a test
dictate responses per endpoint. It is ONLY used by the tests of
:class:`backend.ea_client.EaClient` to exercise the real transport (pagination,
HTTP 401/500, cross-origin redirect, broken/empty JSON bodies, timeouts) — the
per-despacho prohibition on a real calendar is respected because this never
talks to Easy!Appointments.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse


class _Handler(BaseHTTPRequestHandler):
    server_version = ""
    sys_version = ""

    def _dispatch(self):
        server = self.server  # type: ignore[attr-defined]
        parts = urlparse(self.path)
        path = parts.path.rstrip("/") if parts.path != "/" else "/"
        method = self.command
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        body: Any = None
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = raw.decode("utf-8")

        server.record.append(
            {
                "method": method,
                "raw_path": self.path,
                "path": path,
                "query": query,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )

        # A blanket scripted responder (if set) takes full control.
        if server.script_fn is not None:
            return server.script_fn(path, method, query, body, self)

        # Otherwise consult the declarative map.
        spec = server.routes.get((method, path)) or server.routes.get(("*", path))
        if spec is None:
            self.send_response(404)
            self.end_headers()
            return 0
        if isinstance(spec, tuple) and len(spec) == 1:
            spec = spec[0]
        return spec(path, method, query, body, self)

    def do_GET(self):  # noqa: N802
        _safe(self._dispatch)

    def do_POST(self):  # noqa: N802
        _safe(self._dispatch)

    def do_PUT(self):  # noqa: N802
        _safe(self._dispatch)

    def do_DELETE(self):  # noqa: N802
        _safe(self._dispatch)

    def log_message(self, *_: Any) -> None:  # silence accidental logging
        pass


def _safe(fn: Callable) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - a failing handler must not kill the thread
        pass


class _Http:
    def __init__(self, h: BaseHTTPRequestHandler):
        self.h = h

    def send(self, code: int, *, content_type: str = "application/json", payload: Any = "", headers=None):
        data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.h.send_response(code)
        self.h.send_header("Content-Type", content_type)
        self.h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.h.send_header(k, v)
        self.h.end_headers()
        self.h.wfile.write(data)
        return 0


def from_json(payload, *, code: int = 200, handler=None, headers=None) -> int:
    http = _Http(handler)
    return http.send(code, payload=payload, headers=headers)


class EaTestServer:
    """Broadcastable, scriptable EA stand-in."""

    def __init__(self):
        self.routes: dict[tuple | str, Callable] = {}
        self.script_fn: Optional[Callable] = None
        self.record: list[dict] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.routes = self.routes
        self.httpd.record = self.record
        self.httpd.script_fn = None
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def url(self, suffix: str = "") -> str:
        return f"http://127.0.0.1:{self.port}{suffix}"

    def route(self, method: str, path: str, fn: Callable):
        self.routes[(method, path)] = fn

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def paginate_list(keyword_filter, order_fn=None):
    """Build a route for a paginated EA-style list (length/page)."""

    def _route(path, method, query, body, h):
        length = int(query.get("length", 20))
        page = int(query.get("page", 1))
        rows = keyword_filter(query, path)
        rows = list(rows)
        start = (page - 1) * length
        page_rows = rows[start : start + length]
        return from_json(page_rows, handler=h)

    return _route


def hang_forever(_path, _m, _q, _b, handler):
    """Route used to test socket timeout: accept, never answer."""
    try:
        time.sleep(60)
    finally:  # never reached in normal flow
        return 0
