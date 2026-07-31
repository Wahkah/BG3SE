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


def main(argv: list[str] | None = None) -> int:
    try:
        import webview
    except ImportError:
        print("pywebview is not installed.  Install it with:\n"
              "    pip install pywebview", file=sys.stderr)
        return 2

    index = ui_directory() / "index.html"
    if not index.is_file():
        print(f"UI files are missing (expected {index})", file=sys.stderr)
        return 2

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
    webview.start(debug="--debug" in (argv or sys.argv[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
