"""Locating Baldur's Gate 3 savegames and the game install, on any platform.

Windows keeps profiles under %LOCALAPPDATA%, the native macOS build uses
~/Documents, and on Linux the Windows build runs under Proton so the same
AppData tree lives inside a Steam compatdata prefix.  All three are probed.
"""

from __future__ import annotations

import os
import platform
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BG3_STEAM_APPID = "1086940"
GAME_DIR_NAMES = ("Baldurs Gate 3", "Baldur's Gate 3")
_LARIAN = ("Larian Studios", "Baldur's Gate 3")


def _exists(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


# ---------------------------------------------------------------- Steam ----

def _steam_roots() -> list[Path]:
    """Base Steam installation directories to probe, per platform."""
    home = Path.home()
    system = platform.system()
    candidates: list[Path] = []

    if system == "Windows":
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Steam")
        for drive in "CDEFGH":
            candidates.append(Path(f"{drive}:/SteamLibrary"))
            candidates.append(Path(f"{drive}:/Steam"))
    elif system == "Darwin":
        candidates.append(home / "Library/Application Support/Steam")
    else:  # Linux and the BSDs
        candidates += [
            home / ".steam/steam",
            home / ".steam/root",
            home / ".local/share/Steam",
            home / ".var/app/com.valvesoftware.Steam/data/Steam",  # Flatpak
            Path("/usr/share/steam"),
        ]
    return [c for c in candidates if _exists(c)]


def steam_libraries() -> list[Path]:
    """Every Steam library folder, including those on other drives.

    Parses libraryfolders.vdf rather than assuming the default location, which
    is what makes a second-drive install like D:\\SteamLibrary discoverable.
    """
    libraries: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path).lower()
        if key not in seen and _exists(path):
            seen.add(key)
            libraries.append(path)

    for root in _steam_roots():
        add(root)
        for vdf in (root / "steamapps/libraryfolders.vdf",
                    root / "config/libraryfolders.vdf"):
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Entries look like:   "path"    "D:\\SteamLibrary"
            for match in re.finditer(r'"path"\s*"([^"]+)"', text):
                add(Path(match.group(1).replace("\\\\", "\\")))
    return libraries


def game_install_dirs() -> list[Path]:
    """Candidate Baldur's Gate 3 install directories."""
    found: list[Path] = []
    for library in steam_libraries():
        for name in GAME_DIR_NAMES:
            candidate = library / "steamapps/common" / name
            if _exists(candidate):
                found.append(candidate)

    # GOG and manual installs, plus the macOS app bundle layout.
    extra: list[Path] = []
    system = platform.system()
    if system == "Windows":
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                extra += [Path(base) / "GOG Galaxy/Games" / n for n in GAME_DIR_NAMES]
    elif system == "Darwin":
        extra.append(Path("/Applications/Baldur's Gate 3.app/Contents/Resources"))
    for candidate in extra:
        if _exists(candidate):
            found.append(candidate)
    return found


def game_data_dir() -> Path | None:
    """The game's Data directory, which holds the .pak files."""
    for install in game_install_dirs():
        for candidate in (install / "Data", install / "Contents/Data"):
            if _exists(candidate):
                return candidate
    return None


# ------------------------------------------------------------ savegames ----

def profile_roots() -> list[Path]:
    """Directories that contain PlayerProfiles for BG3."""
    home = Path.home()
    system = platform.system()
    roots: list[Path] = []

    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData/Local")
        roots.append(Path(local).joinpath(*_LARIAN))
    elif system == "Darwin":
        roots.append(home / "Documents" / _LARIAN[0] / _LARIAN[1])
        roots.append(home / "Library/Application Support" / _LARIAN[0] / _LARIAN[1])
    else:
        # Native Linux builds do not exist, so look inside the Proton prefixes.
        for library in steam_libraries():
            prefix = library / "steamapps/compatdata" / BG3_STEAM_APPID / "pfx"
            for user in ("steamuser", os.environ.get("USER") or "steamuser"):
                roots.append(
                    prefix / "drive_c/users" / user / "AppData/Local" / _LARIAN[0] / _LARIAN[1]
                )
        # Lutris / wine prefixes people commonly use.
        roots.append(home / ".wine/drive_c/users" /
                     (os.environ.get("USER") or "steamuser") /
                     "AppData/Local" / _LARIAN[0] / _LARIAN[1])

    return [r for r in roots if _exists(r / "PlayerProfiles")]


def profiles() -> list[Path]:
    """Every player profile directory found on this machine."""
    out: list[Path] = []
    for root in profile_roots():
        for entry in sorted((root / "PlayerProfiles").iterdir()):
            if entry.is_dir():
                out.append(entry)
    return out


def save_dirs() -> list[Path]:
    """Every directory that holds savegame folders."""
    out: list[Path] = []
    for profile in profiles():
        savegames = profile / "Savegames"
        if not _exists(savegames):
            continue
        # Story holds campaign saves; other subfolders exist for other modes.
        for entry in sorted(savegames.iterdir()):
            if entry.is_dir():
                out.append(entry)
    return out


@dataclass
class SaveSlot:
    """One savegame on disk."""

    path: Path            # the .lsv file
    folder: Path          # the directory containing it
    name: str             # display name, from the folder
    modified: datetime
    size: int
    profile: str = ""
    mode: str = ""        # "Story", etc.
    screenshot: Path | None = None

    @property
    def id(self) -> str:
        return str(self.path)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": str(self.path),
            "folder": str(self.folder),
            "name": self.name,
            "modified": self.modified.isoformat(),
            "modified_display": self.modified.strftime("%Y-%m-%d %H:%M"),
            "size": self.size,
            "profile": self.profile,
            "mode": self.mode,
            "screenshot": str(self.screenshot) if self.screenshot else None,
        }


def _slot_from_folder(folder: Path, profile: str = "", mode: str = "") -> SaveSlot | None:
    saves = sorted(folder.glob("*.lsv"))
    if not saves:
        return None
    lsv = saves[0]
    stat = lsv.stat()
    shots = list(folder.glob("*.WebP")) + list(folder.glob("*.webp")) + \
        list(folder.glob("*.png"))
    # Folder names look like "Saiveles-26712416241__Arfurs Mansion - 44h 12m".
    display = folder.name.split("__", 1)[-1] if "__" in folder.name else folder.name
    return SaveSlot(
        path=lsv,
        folder=folder,
        name=display,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).astimezone(),
        size=stat.st_size,
        profile=profile,
        mode=mode,
        screenshot=shots[0] if shots else None,
    )


def find_saves() -> list[SaveSlot]:
    """All savegames on this machine, newest first."""
    slots: list[SaveSlot] = []
    for directory in save_dirs():
        profile = directory.parent.parent.name
        for folder in directory.iterdir():
            if not folder.is_dir():
                continue
            slot = _slot_from_folder(folder, profile=profile, mode=directory.name)
            if slot:
                slots.append(slot)
    slots.sort(key=lambda s: s.modified, reverse=True)
    return slots


def saves_in(directory: str | os.PathLike) -> list[SaveSlot]:
    """Savegames under an explicitly chosen directory.

    Accepts either a folder of save folders or a single save folder.
    """
    base = Path(directory)
    slots: list[SaveSlot] = []
    own = _slot_from_folder(base)
    if own:
        slots.append(own)
    if base.is_dir():
        for folder in sorted(base.iterdir()):
            if folder.is_dir():
                slot = _slot_from_folder(folder)
                if slot:
                    slots.append(slot)
    slots.sort(key=lambda s: s.modified, reverse=True)
    return slots


def describe_environment() -> dict:
    """Diagnostics for the UI, so a failed search is explainable."""
    return {
        "platform": platform.system(),
        "python": sys.version.split()[0],
        "steam_libraries": [str(p) for p in steam_libraries()],
        "game_installs": [str(p) for p in game_install_dirs()],
        "data_dir": str(game_data_dir() or ""),
        "profile_roots": [str(p) for p in profile_roots()],
        "save_dirs": [str(p) for p in save_dirs()],
    }
