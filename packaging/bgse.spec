# PyInstaller spec - builds a standalone app for whichever OS it runs on.
#
#   pyinstaller packaging/bgse.spec --noconfirm
#
# Windows -> dist/BG3SaveEditor/BG3SaveEditor.exe
# macOS   -> dist/BG3 Save Editor.app
# Linux   -> dist/BG3SaveEditor/BG3SaveEditor
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
NAME = "BG3SaveEditor"

# The HTML/CSS/JS interface must travel with the binary.
datas = [(str(ROOT / "src" / "bgse" / "ui"), "bgse/ui")]
binaries = []

# pywebview picks its GUI backend at runtime, so the platform bindings need
# to be pulled in explicitly.
hiddenimports = collect_submodules("webview") + ["lz4.block", "lz4.frame", "zstandard"]
if sys.platform == "win32":
    hiddenimports += ["clr_loader", "pythonnet", "webview.platforms.edgechromium"]
    # pythonnet loads Python.Runtime.dll through clr_loader at runtime.  Without
    # collecting their data files and binaries the frozen build raises
    # "Failed to resolve Python.Runtime.Loader.Initialize".  The app falls back
    # to browser mode if this still fails on the target machine.
    for pkg in ("pythonnet", "clr_loader"):
        try:
            pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
            datas += pkg_datas
            binaries += pkg_binaries
            hiddenimports += pkg_hidden
        except Exception:
            pass
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa", "objc", "Foundation", "WebKit"]
else:
    hiddenimports += ["webview.platforms.gtk", "gi"]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PySide6", "PyQt5", "PyQt6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=NAME,
    # Console stays on: if the native window cannot start, the app falls back
    # to browser mode and needs somewhere to print the URL it is serving on.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=sys.platform == "darwin",
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name=NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="BG3 Save Editor.app",
        bundle_identifier="dev.bgse.saveeditor",
        info_plist={
            "CFBundleName": "BG3 Save Editor",
            "CFBundleDisplayName": "Baldur's Gate 3 Save Editor",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
        },
    )
