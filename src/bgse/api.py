"""The bridge the UI calls into.

Every method returns a plain dict so it can cross the webview boundary as JSON.
Failures come back as ``{"ok": False, "error": "..."}`` rather than raising, so
a bad edit surfaces in the interface instead of killing the window.
"""

from __future__ import annotations

import base64
import functools
import traceback
from pathlib import Path

from . import locate
from .model import FIELD_FORMATS, SaveModel
from .save import GLOBALS_FILE, Savegame, backup_root


def guard(fn):
    """Turn exceptions into an error payload the UI can display."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and "ok" in result:
                return result
            return {"ok": True, "data": result}
        except Exception as exc:                                # noqa: BLE001
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=4),
            }
    return wrapper


class Api:
    """Methods exposed to JavaScript as `pywebview.api.*`."""

    def __init__(self) -> None:
        self.model: SaveModel | None = None

    # ------------------------------------------------------------ browsing
    @guard
    def environment(self) -> dict:
        env = locate.describe_environment()
        env["backup_dir"] = str(backup_root())
        return env

    @guard
    def list_saves(self) -> list[dict]:
        return [s.to_dict() for s in locate.find_saves()]

    @guard
    def list_saves_in(self, directory: str) -> list[dict]:
        return [s.to_dict() for s in locate.saves_in(directory)]

    @guard
    def screenshot(self, path: str) -> dict:
        p = Path(path)
        if not p.is_file():
            return {"data": None}
        raw = p.read_bytes()
        kind = "webp" if p.suffix.lower() == ".webp" else "png"
        return {"data": f"data:image/{kind};base64," + base64.b64encode(raw).decode()}

    # -------------------------------------------------------------- saving
    @guard
    def open_save(self, path: str) -> dict:
        save = Savegame.open(path)
        self.model = SaveModel(save)
        return self.overview_payload()

    @guard
    def overview(self) -> dict:
        return self.overview_payload()

    def overview_payload(self) -> dict:
        model = self._require()
        save = model.save
        info = save.save_info
        return {
            "path": str(save.path),
            "name": save.path.stem,
            "files": [
                {"name": f.name, "size": f.size,
                 "kind": "lsf" if f.name.endswith(".lsf") else
                         "json" if f.name.endswith(".json") else
                         "image" if f.name.lower().endswith((".webp", ".png")) else "bin"}
                for f in save.package.files
            ],
            "save_info": {
                "save_name": info.get("Save Name", ""),
                "difficulty": info.get("Difficulty", ""),
                "game_version": info.get("Game Version", ""),
                "current_level": info.get("Current Level", ""),
                "platform": info.get("Platform", ""),
            },
            "party": [m.to_dict() for m in model.party()],
            "lsf_files": model.lsf_names(),
            "dirty": model.dirty,
        }

    @guard
    def close_save(self) -> dict:
        self.model = None
        return {"closed": True}

    @guard
    def save_changes(self, backup: bool = True, new_folder: str = "") -> dict:
        model = self._require()
        if new_folder:
            model.flush()
            return model.save.save_as_new_slot(new_folder, backup=backup)
        return model.write(backup=backup)

    # --------------------------------------------------------------- party
    @guard
    def party(self) -> list[dict]:
        return [m.to_dict() for m in self._require().party()]

    @guard
    def set_experience(self, slot: int, total: int,
                       current_level: int | None = None) -> dict:
        return self._require().set_experience(int(slot), int(total),
                                              None if current_level in (None, "")
                                              else int(current_level))

    @guard
    def class_rows(self, filename: str = GLOBALS_FILE) -> list[dict]:
        return self._require().class_rows(filename)

    @guard
    def set_class_level(self, row: int, entry: int, level: int,
                        filename: str = GLOBALS_FILE) -> dict:
        return self._require().set_class_level(int(row), int(entry),
                                               int(level), filename)

    @guard
    def progressions(self, filename: str = GLOBALS_FILE) -> list[dict]:
        return self._require().progressions(filename)

    @guard
    def set_feat(self, data_row: int, feat_uuid: str,
                 filename: str = GLOBALS_FILE) -> dict:
        return self._require().set_feat(int(data_row), feat_uuid, filename)

    @guard
    def available_feats(self) -> list[dict]:
        return self._require().available_feats()

    @guard
    def abilities(self, row: int, filename: str = GLOBALS_FILE) -> list[dict]:
        return self._require().abilities(int(row), filename)

    @guard
    def set_ability(self, row: int, index: int, value: int,
                    filename: str = GLOBALS_FILE) -> dict:
        return self._require().set_ability(int(row), int(index),
                                           int(value), filename)

    @guard
    def race_rows(self, filename: str = GLOBALS_FILE) -> list[dict]:
        return self._require().race_rows(filename)

    @guard
    def gamedata(self) -> dict:
        from . import gamedata as gd
        return gd.shared().summary()

    @guard
    def set_save_name(self, name: str) -> dict:
        model = self._require()
        model.save.save_info["Save Name"] = name
        model.save.touch("SaveInfo.json")
        return {"save_name": name}

    # ----------------------------------------------------------------- ECS
    @guard
    def ecs_files(self) -> list[str]:
        return self._require().ecs_files()

    @guard
    def ecs_summary(self, filename: str = GLOBALS_FILE) -> dict:
        ecs = self._require().ecs(filename)
        return ecs.summary()

    @guard
    def component_types(self, filename: str = GLOBALS_FILE, query: str = "",
                        populated_only: bool = True) -> list[dict]:
        return self._require().component_types(filename, query, populated_only)

    @guard
    def component_rows(self, filename: str, name: str, start: int = 0,
                       limit: int = 50) -> dict:
        return self._require().component_rows(filename, name, int(start), int(limit))

    @guard
    def set_component_field(self, filename: str, name: str, index: int,
                            offset: int, kind: str, value: float) -> dict:
        return self._require().set_component_field(
            filename, name, int(index), int(offset), kind, value)

    @guard
    def field_kinds(self) -> list[str]:
        return list(FIELD_FORMATS)

    # ------------------------------------------------------------ raw tree
    @guard
    def tree_children(self, filename: str, path: str = "") -> list[dict]:
        return self._require().tree_children(filename, path)

    @guard
    def node_detail(self, filename: str, path: str) -> dict:
        return self._require().node_detail(filename, path)

    @guard
    def set_attribute(self, filename: str, path: str, attr: str, value) -> dict:
        return self._require().set_attribute(filename, path, attr, value)

    @guard
    def search(self, filename: str, needle: str, limit: int = 200) -> list[dict]:
        return self._require().search(filename, needle, int(limit))

    # ------------------------------------------------------------- helpers
    def _require(self) -> SaveModel:
        if self.model is None:
            raise RuntimeError("no savegame is open")
        return self.model
