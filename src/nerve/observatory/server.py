from __future__ import annotations

import json
import mimetypes
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .read_store import ObservatoryReadStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3000
STATIC_DIR = Path(__file__).with_name("static")


class ObservatoryServer(ThreadingHTTPServer):
    db_path: Path


class ObservatoryHandler(BaseHTTPRequestHandler):
    server: ObservatoryServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/observatory/"):
            self._serve_api(parsed.path, parse_qs(parsed.query))
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        self._reject_mutation()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_mutation()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_mutation()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_mutation()

    def _reject_mutation(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"error":"Observatory API is read-only"}')

    def _serve_api(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            with ObservatoryReadStore(self.server.db_path) as store:
                payload: Any
                if path == "/api/observatory/summary":
                    payload = store.summary()
                elif path == "/api/observatory/snapshots":
                    try:
                        limit = int(query.get("limit", ["100"])[0])
                    except ValueError:
                        self._json({"error": "limit must be an integer"}, HTTPStatus.BAD_REQUEST)
                        return
                    payload = store.recent_snapshots(limit)
                elif path.startswith("/api/observatory/snapshot/"):
                    snapshot_id = unquote(path.removeprefix("/api/observatory/snapshot/"))
                    payload = store.snapshot_detail(snapshot_id)
                    if payload is None:
                        self._json({"error": "snapshot not found"}, HTTPStatus.NOT_FOUND)
                        return
                elif path == "/api/observatory/arms":
                    payload = store.arm_comparison()
                elif path == "/api/observatory/calibration":
                    payload = store.calibration()
                elif path == "/api/observatory/health":
                    payload = store.health()
                elif path == "/api/observatory/memecoin-state":
                    payload = store.memecoin_state_summary()
                else:
                    self._json({"error": "endpoint not found"}, HTTPStatus.NOT_FOUND)
                    return
        except (OSError, sqlite3.Error) as exc:
            self._json(
                {"error": "truth store unavailable", "detail": str(exc)},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._json(payload)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_static(self, path: str) -> None:
        files = {
            "/": "index.html",
            "/index.html": "index.html",
            "/style.css": "style.css",
            "/app.js": "app.js",
        }
        filename = files.get(path)
        if filename is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = (STATIC_DIR / filename).read_bytes()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return

def make_server(
    db_path: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> ObservatoryServer:
    server = ObservatoryServer((host, port), ObservatoryHandler)
    server.db_path = db_path
    return server


def serve(db_path: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"NERVE truth store does not exist: {db_path}")
    with make_server(db_path, host, port) as server:
        print(f"NERVE Observatory: http://{host}:{server.server_port}", flush=True)
        print(f"Truth Store: READ ONLY · {db_path}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return
