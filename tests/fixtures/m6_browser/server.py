#!/usr/bin/env python3
"""Serve a disposable mounted M6 API and the actual dashboard bundle on loopback."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pytest import MonkeyPatch

PREFIX = "/api/plugins/local-first-orchestrator"


class HarnessServer(ThreadingHTTPServer):
    fixture_base: str
    bundle: Path
    plugin: Path
    board: object


class Handler(BaseHTTPRequestHandler):
    server: HarnessServer

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def _reply(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/__fixture/writes":
            self._reply(200, json.dumps(self.server.board.writes).encode(), "application/json; charset=utf-8")
        elif path.startswith("/api/"):
            self._proxy_fixture(path)
        elif path == "/":
            self._reply(200, HTML.encode(), "text/html; charset=utf-8")
        elif path == "/harness.js":
            self._reply(200, self.server.bundle.read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/actual-plugin/index.js":
            self._reply(200, self.server.plugin.read_bytes(), "text/javascript; charset=utf-8")
        else:
            self._reply(404, b"not found\n", "text/plain; charset=utf-8")

    def _proxy_fixture(self, path: str) -> None:
        if not path.startswith(PREFIX):
            self._reply(404, b'{"detail":"fixture route unavailable"}', "application/json")
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length else None
        request = Request(
            self.server.fixture_base + self.path,
            data=payload,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
        )
        try:
            with urlopen(request, timeout=5) as response:
                body, code = response.read(), response.status
        except HTTPError as error:
            body, code = error.read(), error.code
        self._reply(code, body, "application/json; charset=utf-8")


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>M6 browser fixture</title>
<style>body{font:14px system-ui;margin:0;background:#0d1117;color:#e6edf3}#root{padding:24px;max-width:1100px;margin:auto}.m6-card{background:#161b22;border:1px solid #30363d;border-radius:8px;margin:0 0 16px}.m6-card-header{padding:16px 16px 0}.m6-card-title{font-size:18px;margin:0}.m6-card-content{padding:16px}.m6-button{background:#238636;color:#fff;border:0;border-radius:6px;padding:8px 12px;margin:2px;cursor:pointer}.m6-button:disabled{opacity:.55;cursor:not-allowed}.m6-badge{background:#1f6feb;border-radius:999px;padding:4px 8px}pre{white-space:pre-wrap}.grid{display:grid}</style>
</head><body><main id="root"></main><script src="/harness.js"></script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    repository = args.repository.resolve()
    plugin = repository / "dashboard" / "dist" / "index.js"
    if not plugin.is_file() or not args.bundle.is_file():
        raise SystemExit("M6 harness requires the shipped bundle and a generated React harness bundle")
    sys.path.insert(0, str(repository))
    from tests.test_operator_api import mounted_api

    os.environ["HERMES_M0_CLI"] = ""
    monkeypatch = MonkeyPatch()
    with tempfile.TemporaryDirectory(prefix="m6-browser-") as temporary:
        generator = mounted_api.__wrapped__(Path(temporary), monkeypatch)
        fixture_base, board = next(generator)
        server = HarnessServer(("127.0.0.1", 0), Handler)
        server.fixture_base, server.bundle, server.plugin, server.board = fixture_base, args.bundle, plugin, board
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/", "fixture_backend": fixture_base}), flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            generator.close()
            monkeypatch.undo()


if __name__ == "__main__":
    main()
