"""Desktop entry point: a native window hosting the editor UI.

pywebview uses the platform's own web view - WebView2 on Windows, WKWebView on
macOS, WebKitGTK on Linux - so the packaged app stays small and looks native on
each OS.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .api import Api

WINDOW_TITLE = "Baldur's Gate 3 Save Editor"


def ui_directory() -> Path:
    """Locate the bundled UI, both in-tree and inside a PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        bundled = Path(base) / "bgse" / "ui"
        if bundled.is_dir():
            return bundled
        return Path(base) / "ui"
    return Path(__file__).resolve().parent / "ui"


def _fallback_to_browser(reason: str) -> int:
    """Serve the same UI over loopback when no native window is available.

    pywebview needs a platform backend - pythonnet/CLR on Windows, WebKitGTK on
    Linux - and those are exactly the parts that fail in a frozen build or on a
    machine missing system libraries.  The HTTP bridge has no such dependency,
    so falling back keeps the app usable instead of dying with a traceback.
    """
    print(f"Native window unavailable ({reason}).", file=sys.stderr)
    print("Falling back to browser mode.", file=sys.stderr)
    from .webapp import main as web_main

    return web_main(open_browser=True)


def main(argv: list[str] | None = None) -> int:
    index = ui_directory() / "index.html"
    if not index.is_file():
        print(f"UI files are missing (expected {index})", file=sys.stderr)
        return 2

    argv = argv if argv is not None else sys.argv[1:]
    if "--web" in argv:
        from .webapp import main as web_main
        return web_main(open_browser=True)

    try:
        import webview
    except Exception as exc:                                    # noqa: BLE001
        return _fallback_to_browser(f"pywebview import failed: {exc}")

    try:
        api = Api()
        webview.create_window(
            WINDOW_TITLE,
            str(index),
            js_api=api,
            width=1320,
            height=880,
            min_size=(1000, 650),
            background_color="#14110E",
        )
        # debug=True opens the platform inspector; handy when hacking on the UI.
        webview.start(debug="--debug" in argv)
    except Exception as exc:                                    # noqa: BLE001
        # Backend resolution failures surface here, not at import time.
        return _fallback_to_browser(f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
