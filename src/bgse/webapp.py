"""Serve the editor UI over local HTTP instead of a native window.

Useful in three situations: previewing the interface in an ordinary browser,
running on a machine where no system webview is available (some Linux setups),
and driving the UI from tests.

The bridge mirrors the pywebview one exactly - a POST to /api/<method> with a
JSON array of arguments calls the matching :class:`~bgse.api.Api` method - so
the same `app.js` drives both.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api import Api

UI_DIR = Path(__file__).resolve().parent / "ui"

#: Only one savegame is open at a time, so serialise calls into the model.
_lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    api: Api = None            # set on the server class below

    def log_message(self, fmt, *args):        # noqa: A003 - silence the default logging
        pass

    # ------------------------------------------------------------ responses
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    # ----------------------------------------------------------------- HTTP
    def do_GET(self) -> None:
        rel = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        target = (UI_DIR / rel).resolve()
        # Never serve anything outside the UI directory.
        if not target.is_file() or UI_DIR not in target.parents:
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self) -> None:
        if not self.path.startswith("/api/"):
            self._send(404, b"not found", "text/plain")
            return
        method = self.path[len("/api/"):].split("?", 1)[0]
        fn = getattr(type(self).api, method, None)
        if not callable(fn) or method.startswith("_"):
            self._json({"ok": False, "error": f"no such method {method!r}"}, 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"[]"
        try:
            args = json.loads(raw or b"[]")
        except json.JSONDecodeError as exc:
            self._json({"ok": False, "error": f"bad request body: {exc}"}, 400)
            return
        if not isinstance(args, list):
            args = [args]

        with _lock:
            result = fn(*args)
        self._json(result)


def serve(host: str = "127.0.0.1", port: int = 0, open_browser: bool = True,
          api: Api | None = None) -> ThreadingHTTPServer:
    """Start the HTTP bridge.  Binds to loopback only."""
    handler = type("Handler", (_Handler,), {"api": api or Api()})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{httpd.server_port}/"
    print(f"BG3 Save Editor UI: {url}")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    return httpd


def main(host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> int:
    httpd = serve(host, port, open_browser)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
