"""Local fake POST /api/reservar. Serves dist/ on 127.0.0.1 only.

Never proxies to Easy!Appointments or any production host.
Switch the next response with GET /_fake/outcome?set=success|conflict|invalid|down
"""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
HOST = "127.0.0.1"
PORT = 8787

LOCK = threading.Lock()
STATE = {
    "outcome": "success",
    "delay_ms": 400,
    "last": None,
    "posts": 0,
}


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "EquilibraFakeReservar/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[fake-reservar] " + (fmt % args) + "\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def do_OPTIONS(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/reservar":
            self.send_error(404)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8787")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/_fake/outcome":
            qs = parse_qs(parsed.query)
            values = qs.get("set") or qs.get("outcome") or []
            if values:
                outcome = values[0].strip().lower()
                if outcome not in {"success", "conflict", "invalid", "down"}:
                    self._send_json(400, {"ok": False, "error": "unknown outcome"})
                    return
                with LOCK:
                    STATE["outcome"] = outcome
            with LOCK:
                current = dict(STATE)
                current.pop("last", None)
            self._send_json(200, {"ok": True, "outcome": current["outcome"]})
            return
        if parsed.path == "/_fake/last":
            with LOCK:
                payload = {"ok": True, "posts": STATE["posts"], "last": STATE["last"]}
            self._send_json(200, payload)
            return

        rel = parsed.path.lstrip("/")
        if rel == "" or rel.endswith("/"):
            rel = rel + "index.html"
        full = (DIST / rel).resolve()
        try:
            full.relative_to(DIST.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not full.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        if full.suffix == ".woff2":
            ctype = "font/woff2"
        self._send(200, full.read_bytes(), ctype)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/reservar":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "code": "invalid_request", "fields": []})
            return
        with LOCK:
            STATE["posts"] += 1
            STATE["last"] = data
            outcome = STATE["outcome"]
            delay_ms = STATE["delay_ms"]
        if delay_ms:
            time.sleep(delay_ms / 1000)
        if outcome == "conflict":
            self._send_json(409, {"ok": False, "code": "requested_hour_is_unavailable"})
            return
        if outcome == "invalid":
            self._send_json(400, {"ok": False, "code": "invalid_request", "fields": ["email"]})
            return
        if outcome == "down":
            self._send_json(502, {"ok": False, "code": "upstream_unavailable"})
            return
        self._send_json(201, {"ok": True, "appointment_id": 9001})


def main() -> int:
    if not DIST.is_dir():
        sys.stderr.write("[fake-reservar] dist/ is missing. Run scripts/build.ps1 first.\n")
        return 1
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"[fake-reservar] http://{HOST}:{PORT}/turnos/  (outcome={STATE['outcome']})\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
