"""Resolve Larian UUIDs to readable names using the installed game data.

Classes, subclasses, races, feats and backgrounds are defined in .lsx files
(plain XML) inside the game's .pak archives.  Each definition node carries a
``UUID`` and a ``Name``, and subclasses point at their parent class through
``ParentGuid``, which is enough to turn the raw GUIDs stored in a savegame into
labels like "Fighter (Champion)".

Archives are opened lazily - only the header and file list are read - so
indexing a 13 GB pak costs a fraction of a second.  The result is cached on
disk because parsing every definition file takes a few seconds.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from . import locate
from .formats.lspk import Package
from .save import app_data_dir

CACHE_VERSION = 2

#: filename -> category.  Definition files live under several mod folders and
#: are merged; a UUID means the same thing wherever it is defined.
DEFINITION_FILES = {
    "classdescriptions.lsx": "class",
    "races.lsx": "race",
    "feats.lsx": "feat",
    "featdescriptions.lsx": "feat",
    "backgrounds.lsx": "background",
    "gods.lsx": "god",
    "origins.lsx": "origin",
    "progressiondescriptions.lsx": "progression",
}

#: Archives worth scanning.  The huge texture/sound paks hold no definitions.
CANDIDATE_PAKS = (
    "Shared.pak", "GustavX.pak", "Gustav.pak", "Game.pak",
    "Patch8_HotFix8.pak", "Assets.pak",
)


#: Root templates define every placeable object, including items.  They live in
#: merged .lsf files rather than .lsx, so they are catalogued separately.
ROOT_TEMPLATE_MARKER = "roottemplates"
ROOT_TEMPLATE_PAKS = ("Shared.pak", "Gustav.pak", "GustavX.pak", "Game.pak")


@dataclass
class Definition:
    uuid: str
    name: str
    category: str
    parent: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "name": self.name, "category": self.category,
                "parent": self.parent, "source": self.source}


@dataclass
class GameData:
    """UUID -> definition, across every category."""

    entries: dict[str, Definition] = field(default_factory=dict)
    data_dir: str = ""
    scanned: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- lookup
    def get(self, uuid: str) -> Definition | None:
        return self.entries.get((uuid or "").lower())

    def name(self, uuid: str, default: str = "") -> str:
        entry = self.get(uuid)
        return entry.name if entry else default

    def of_category(self, category: str) -> list[Definition]:
        return sorted((e for e in self.entries.values() if e.category == category),
                      key=lambda e: e.name)

    def class_label(self, class_uuid: str, subclass_uuid: str = "") -> str:
        """Render a class/subclass pair the way the game names it."""
        main = self.get(class_uuid)
        sub = self.get(subclass_uuid) if subclass_uuid else None
        # A subclass records its parent, so use it when the class slot is empty.
        if main is None and sub is not None and sub.parent:
            main = self.get(sub.parent)
        if main is None and sub is None:
            return ""
        if main is None:
            return sub.name
        return f"{main.name} ({sub.name})" if sub else main.name

    @property
    def empty(self) -> bool:
        return not self.entries

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for entry in self.entries.values():
            counts[entry.category] = counts.get(entry.category, 0) + 1
        return {"data_dir": self.data_dir, "total": len(self.entries),
                "by_category": counts, "archives": self.scanned}

    # -------------------------------------------------------------- build
    @classmethod
    def load(cls, data_dir: str | Path | None = None,
             use_cache: bool = True) -> "GameData":
        directory = Path(data_dir) if data_dir else locate.game_data_dir()
        if directory is None or not directory.is_dir():
            return cls()

        cache_file = app_data_dir() / "gamedata.json"
        stamp = _fingerprint(directory)
        if use_cache:
            cached = _read_cache(cache_file, stamp)
            if cached is not None:
                return cached

        data = cls(data_dir=str(directory))
        for pak_name in CANDIDATE_PAKS:
            path = directory / pak_name
            if not path.is_file():
                continue
            try:
                package = Package.open(path)
            except Exception:                                   # noqa: BLE001
                continue
            data.scanned.append(pak_name)
            for entry in package.files:
                base = entry.name.rsplit("/", 1)[-1].lower()
                category = DEFINITION_FILES.get(base)
                if category is None or not entry.name.lower().endswith(".lsx"):
                    continue
                try:
                    blob = entry.data
                except Exception:                               # noqa: BLE001
                    continue
                data._absorb(blob, category, f"{pak_name}:{entry.name}")

        _write_cache(cache_file, stamp, data)
        return data

    def _absorb(self, blob: bytes, category: str, source: str) -> None:
        try:
            root = ElementTree.fromstring(blob.decode("utf-8", "replace"))
        except ElementTree.ParseError:
            return
        for node in root.iter("node"):
            values: dict[str, str] = {}
            for attr in node.findall("attribute"):
                key = attr.get("id")
                if key in ("UUID", "Name", "ParentGuid", "Object", "DisplayName"):
                    values[key] = attr.get("value") or attr.get("handle") or ""
            uuid = (values.get("UUID") or values.get("Object") or "").lower()
            name = values.get("Name") or ""
            if not uuid or not name:
                continue
            # Later archives refine earlier ones; keep the first real name but
            # let a definition with a parent replace a parentless duplicate.
            existing = self.entries.get(uuid)
            parent = (values.get("ParentGuid") or "").lower()
            if existing is None or (parent and not existing.parent):
                self.entries[uuid] = Definition(uuid=uuid, name=name,
                                                category=category,
                                                parent=parent, source=source)


# ------------------------------------------------------------------ caching

def _fingerprint(directory: Path) -> str:
    parts = [str(CACHE_VERSION), str(directory)]
    for pak in CANDIDATE_PAKS:
        path = directory / pak
        if path.is_file():
            stat = path.stat()
            parts.append(f"{pak}:{stat.st_size}:{int(stat.st_mtime)}")
    return "|".join(parts)


def _read_cache(path: Path, stamp: str) -> GameData | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("stamp") != stamp:
        return None
    data = GameData(data_dir=payload.get("data_dir", ""),
                    scanned=payload.get("archives", []))
    for row in payload.get("entries", []):
        data.entries[row["uuid"]] = Definition(**row)
    return data


def _write_cache(path: Path, stamp: str, data: GameData) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "stamp": stamp,
            "data_dir": data.data_dir,
            "archives": data.scanned,
            "built": int(time.time()),
            "entries": [e.to_dict() for e in data.entries.values()],
        }), encoding="utf-8")
    except OSError:
        pass


_cached: GameData | None = None


def shared() -> GameData:
    """Process-wide instance, built on first use."""
    global _cached
    if _cached is None:
        _cached = GameData.load()
    return _cached


# ------------------------------------------------------------ root templates

@dataclass
class RootTemplate:
    uuid: str
    name: str
    type: str = ""
    stats: str = ""

    def to_dict(self) -> dict:
        return {"uuid": self.uuid, "name": self.name,
                "type": self.type, "stats": self.stats}


def _load_root_templates(directory: Path) -> dict[str, RootTemplate]:
    from .formats import lsf                       # local: keeps import cost off startup
    from .formats.lspk import Package

    out: dict[str, RootTemplate] = {}
    for pak_name in ROOT_TEMPLATE_PAKS:
        path = directory / pak_name
        if not path.is_file():
            continue
        try:
            package = Package.open(path)
        except Exception:                                       # noqa: BLE001
            continue
        for entry in package.files:
            lowered = entry.name.lower()
            if ROOT_TEMPLATE_MARKER not in lowered or not lowered.endswith(".lsf"):
                continue
            try:
                doc = lsf.LSFDocument.from_bytes(entry.data)
            except Exception:                                   # noqa: BLE001
                continue
            for region in doc.resource.regions.values():
                for node in region.walk():
                    if node.name != "GameObjects":
                        continue
                    key = node.get("MapKey")
                    if not key:
                        continue
                    out[str(key).lower()] = RootTemplate(
                        uuid=str(key).lower(),
                        name=str(node.get("Name") or ""),
                        type=str(node.get("Type") or ""),
                        stats=str(node.get("Stats") or ""),
                    )
    return out


_templates: dict[str, RootTemplate] | None = None


def root_templates(data_dir: str | Path | None = None,
                   use_cache: bool = True) -> dict[str, RootTemplate]:
    """UUID -> root template, for naming the items found in a savegame."""
    global _templates
    if _templates is not None:
        return _templates

    directory = Path(data_dir) if data_dir else locate.game_data_dir()
    if directory is None or not directory.is_dir():
        _templates = {}
        return _templates

    cache_file = app_data_dir() / "roottemplates.json"
    stamp = _fingerprint(directory) + "|rt"
    if use_cache:
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if payload.get("stamp") == stamp:
                _templates = {r["uuid"]: RootTemplate(**r)
                              for r in payload.get("templates", [])}
                return _templates
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    _templates = _load_root_templates(directory)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "stamp": stamp,
            "templates": [t.to_dict() for t in _templates.values()],
        }), encoding="utf-8")
    except OSError:
        pass
    return _templates


def template_name(uuid: str, default: str = "") -> str:
    entry = root_templates().get((uuid or "").lower())
    return entry.name if entry else default
