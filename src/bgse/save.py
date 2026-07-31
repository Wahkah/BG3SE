"""High-level savegame access: open a .lsv, edit it, write it back safely.

Every write goes through a temp file and an atomic replace, and the untouched
original is copied into a timestamped backup outside the savegames tree first,
so a bad edit never costs a playthrough.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from datetime import datetime
from pathlib import Path

from .formats import lsf
from .formats.lspk import Package, PackagedFile

META_FILE = "meta.lsf"
GLOBALS_FILE = "Globals.lsf"
SAVE_INFO_FILE = "SaveInfo.json"
STORY_FILE = "StorySave.bin"


def app_data_dir() -> Path:
    """Where bgse keeps backups and settings, per platform convention."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "bgse"
    if system == "Darwin":
        return Path.home() / "Library/Application Support/bgse"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "bgse"


def backup_root() -> Path:
    return app_data_dir() / "backups"


class SaveError(Exception):
    pass


class Savegame:
    """A Baldur's Gate 3 savegame package.

    Sub-documents are parsed on first access; Globals.lsf in particular takes a
    few seconds, so it is never touched unless something actually needs it.
    """

    def __init__(self, path: str | os.PathLike, package: Package):
        self.path = Path(path)
        self.package = package
        self._meta: lsf.LSFDocument | None = None
        self._globals: lsf.LSFDocument | None = None
        self._save_info: dict | None = None
        self._dirty: set[str] = set()

    # ------------------------------------------------------------ loading
    @classmethod
    def open(cls, path: str | os.PathLike) -> "Savegame":
        path = Path(path)
        if path.is_dir():
            candidates = sorted(path.glob("*.lsv"))
            if not candidates:
                raise SaveError(f"no .lsv file inside {path}")
            path = candidates[0]
        try:
            package = Package.read(path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            raise SaveError(f"could not read {path.name}: {exc}") from exc
        return cls(path, package)

    # --------------------------------------------------------- documents
    @property
    def meta(self) -> lsf.LSFDocument:
        if self._meta is None:
            entry = self.package.get(META_FILE)
            if entry is None:
                raise SaveError(f"{self.path.name} has no {META_FILE}")
            self._meta = lsf.LSFDocument.from_bytes(entry.data)
        return self._meta

    @property
    def globals(self) -> lsf.LSFDocument:
        if self._globals is None:
            entry = self.package.get(GLOBALS_FILE)
            if entry is None:
                raise SaveError(f"{self.path.name} has no {GLOBALS_FILE}")
            self._globals = lsf.LSFDocument.from_bytes(entry.data)
        return self._globals

    @property
    def globals_loaded(self) -> bool:
        return self._globals is not None

    @property
    def save_info(self) -> dict:
        if self._save_info is None:
            entry = self.package.get(SAVE_INFO_FILE)
            if entry is None:
                self._save_info = {}
            else:
                try:
                    self._save_info = json.loads(entry.data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._save_info = {}
        return self._save_info

    @property
    def screenshot(self) -> bytes | None:
        for entry in self.package.files:
            if entry.name.lower().endswith((".webp", ".png")):
                return entry.data
        return None

    @property
    def screenshot_name(self) -> str | None:
        for entry in self.package.files:
            if entry.name.lower().endswith((".webp", ".png")):
                return entry.name
        return None

    # ----------------------------------------------------------- editing
    def touch(self, which: str) -> None:
        """Mark a sub-document as modified so it gets re-serialised on save."""
        self._dirty.add(which)

    @property
    def dirty(self) -> bool:
        return bool(self._dirty)

    def _flush(self) -> None:
        """Serialise modified sub-documents back into the package."""
        if META_FILE in self._dirty and self._meta is not None:
            self.package[META_FILE].data = self._meta.to_bytes()
        if GLOBALS_FILE in self._dirty and self._globals is not None:
            self.package[GLOBALS_FILE].data = self._globals.to_bytes()
        if SAVE_INFO_FILE in self._dirty and self._save_info is not None:
            entry = self.package.get(SAVE_INFO_FILE)
            if entry is not None:
                entry.data = json.dumps(self._save_info, indent=3).encode("utf-8")
        self._dirty.clear()

    # ----------------------------------------------------------- writing
    def backup(self) -> Path:
        """Copy the current on-disk save into a timestamped backup folder."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target_dir = backup_root() / self.path.parent.name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{self.path.stem}-{stamp}.lsv"
        shutil.copy2(self.path, target)
        return target

    def save(self, path: str | os.PathLike | None = None,
             backup: bool = True) -> dict:
        """Write the package back to disk atomically.

        Returns a summary describing what happened, for display in the UI.
        """
        target = Path(path) if path is not None else self.path
        overwriting = target.exists()

        backup_path = None
        if backup and overwriting and target == self.path:
            backup_path = self.backup()

        self._flush()
        blob = self.package.to_bytes()

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".bgse-tmp")
        try:
            tmp.write_bytes(blob)
            os.replace(tmp, target)          # atomic on both NT and POSIX
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        self.path = target
        return {
            "path": str(target),
            "bytes": len(blob),
            "backup": str(backup_path) if backup_path else None,
        }

    def save_as_new_slot(self, folder_name: str, backup: bool = True) -> dict:
        """Write to a sibling save folder, copying the screenshot alongside.

        BG3 expects `<Savegames>/<Mode>/<Folder>/<Name>.lsv` plus a matching
        preview image, so both are created.
        """
        target_dir = self.path.parent.parent / folder_name
        target_dir.mkdir(parents=True, exist_ok=True)
        display = folder_name.split("__", 1)[-1] if "__" in folder_name else folder_name
        result = self.save(target_dir / f"{display}.lsv", backup=backup)

        shot = self.screenshot
        if shot is not None:
            suffix = Path(self.screenshot_name or "preview.WebP").suffix
            (target_dir / f"{display}{suffix}").write_bytes(shot)
        return result

    def __repr__(self) -> str:
        return f"<Savegame {self.path.name} files={len(self.package.files)}>"
