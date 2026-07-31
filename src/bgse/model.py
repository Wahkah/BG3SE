"""Domain model: a friendly view over a savegame's LSF trees and ECS blob.

The ECS blob is where BG3 keeps character data.  Component layouts are not
documented by Larian, so this module exposes two levels:

* named accessors for components whose layout has been confirmed against real
  saves (experience, for example, is verified against SaveInfo.json), and
* a generic component browser that can read and patch any of the ~350 component
  arrays field by field, which is what makes the rest reachable.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import gamedata
from .formats import lsf, lsmf
from .formats.lsf import decode_uuid, encode_uuid
from .formats.resource import DataType, Node
from .save import GLOBALS_FILE, Savegame

#: Component holding per-character experience: int32 current, int32 total, int32 spare.
EXPERIENCE = "game.experience.v0.ExperienceComponent"
AVAILABLE_LEVEL = "game.experience.v0.AvailableLevelComponent"

#: Class levels live in a heap payload of fixed 40-byte records.
CLASSES = "game.stats.v0.ClassesComponent"
CLASS_ENTRY = struct.Struct("<16s16sII")   # class, subclass, level, unknown
NIL_UUID = "00000000-0000-0000-0000-000000000000"

#: One record per level taken.  The first three fields are the class chosen at
#: that level, the subclass (only on the level it is picked) and the feat.
LEVELUP = "game.character_creation.v3.LevelUpComponent"
LEVELUP_DATA = "game.character_creation.v3.LevelUpComponentData"
FEAT_OFFSET = 32
RACE = "game.race.v0.RaceComponent"

#: Ability scores: six int32 at offset 8 of a 36-byte element.
STATS = "game.stats.v3.StatsComponent"
ABILITY_OFFSET = 8
ABILITY_NAMES = ("Strength", "Dexterity", "Constitution",
                 "Intelligence", "Wisdom", "Charisma")
ABILITY_SHORT = ("STR", "DEX", "CON", "INT", "WIS", "CHA")

#: Components with this many elements are parallel arrays over the same
#: entities, so one matched row index reads across all of them.
PARALLEL_COMPONENTS = (STATS, CLASSES, RACE)

#: Struct codes offered by the generic component editor.
FIELD_FORMATS = {
    "int8": "<b", "uint8": "<B", "int16": "<h", "uint16": "<H",
    "int32": "<i", "uint32": "<I", "int64": "<q", "uint64": "<Q",
    "float": "<f", "double": "<d",
}


@dataclass
class PartyMember:
    """A party member, assembled from SaveInfo.json plus the ECS."""

    index: int
    name: str = ""
    origin: str = ""
    race: str = ""
    level: int = 0
    classes: list[dict] = field(default_factory=list)
    xp_total: int = 0
    xp_current_level: int = 0
    subregion: str = ""
    #: index into the ExperienceComponent array, when it could be matched
    xp_slot: int | None = None
    #: index into the ClassesComponent array, when it could be matched
    class_row: int | None = None
    #: decoded class levels from the ECS (richer than the save summary)
    class_levels: list[dict] = field(default_factory=list)
    #: index into the LevelUpComponent array, when it could be matched
    progression_row: int | None = None
    #: feats taken, one entry per level-up that granted one
    feats: list[dict] = field(default_factory=list)
    #: six ability scores, read from the row matched via the class component
    abilities: list[dict] = field(default_factory=list)
    #: race as named by the game data
    race_name: str = ""

    @property
    def class_label(self) -> str:
        parts = []
        for c in self.classes:
            main, sub = c.get("Main", ""), c.get("Sub", "")
            parts.append(f"{main} ({sub})" if sub else main)
        # Summons and familiars appear in the party list with no class data.
        return " / ".join(parts) or "No class data"

    def to_dict(self) -> dict:
        return {
            "index": self.index, "name": self.name, "origin": self.origin,
            "race": self.race, "level": self.level, "classes": self.classes,
            "class_label": self.class_label, "xp_total": self.xp_total,
            "xp_current_level": self.xp_current_level,
            "subregion": self.subregion, "xp_slot": self.xp_slot,
            "editable_xp": self.xp_slot is not None,
            "class_row": self.class_row,
            "class_levels": self.class_levels,
            "editable_classes": self.class_row is not None,
            "progression_row": self.progression_row,
            "feats": self.feats,
            "editable_feats": self.progression_row is not None,
            "abilities": self.abilities,
            "race_name": self.race_name,
            "editable_abilities": bool(self.abilities),
        }


class SaveModel:
    """Editing façade over one savegame."""

    def __init__(self, save: Savegame):
        self.save = save
        self._ecs: dict[str, lsmf.LSMFDocument] = {}
        self._docs: dict[str, lsf.LSFDocument] = {}
        self._ecs_dirty: set[str] = set()
        self._dirty_lsf: set[str] = set()

    # ------------------------------------------------------------- loading
    def lsf_document(self, filename: str) -> lsf.LSFDocument:
        """Parse (and cache) any LSF inside the package."""
        if filename not in self._docs:
            if filename == GLOBALS_FILE:
                self._docs[filename] = self.save.globals
            else:
                entry = self.save.package.get(filename)
                if entry is None:
                    raise KeyError(filename)
                self._docs[filename] = lsf.LSFDocument.from_bytes(entry.data)
        return self._docs[filename]

    def lsf_names(self) -> list[str]:
        return [f.name for f in self.save.package.files if f.name.endswith(".lsf")]

    def ecs(self, filename: str = GLOBALS_FILE) -> lsmf.LSMFDocument:
        """The ECS blob stored in a file's NewAge region."""
        if filename not in self._ecs:
            doc = self.lsf_document(filename)
            region = doc.resource.regions.get("NewAge")
            if region is None:
                raise KeyError(f"{filename} has no NewAge region")
            self._ecs[filename] = lsmf.LSMFDocument.from_bytes(region.get("NewAge"))
        return self._ecs[filename]

    def ecs_files(self) -> list[str]:
        """LSF files that carry an ECS blob."""
        out = []
        for name in self.lsf_names():
            try:
                doc = self.lsf_document(name)
            except Exception:                                   # noqa: BLE001
                continue
            if "NewAge" in doc.resource.regions:
                out.append(name)
        return out

    # -------------------------------------------------------------- party
    def party(self) -> list[PartyMember]:
        """Party members from SaveInfo.json, matched to their ECS experience row."""
        info = self.save.save_info.get("Active Party", {}).get("Characters", [])
        members: list[PartyMember] = []
        for i, entry in enumerate(info):
            members.append(PartyMember(
                index=i,
                origin=entry.get("Origin", ""),
                race=entry.get("Race", ""),
                level=int(entry.get("Level", 0) or 0),
                classes=entry.get("Classes", []) or [],
                xp_total=int(entry.get("Experience Points (Total)", 0) or 0),
                xp_current_level=int(
                    entry.get("Experience Points (Current level)", 0) or 0),
                subregion=entry.get("Subregion", ""),
            ))
            members[-1].name = members[-1].origin or f"Character {i + 1}"

        # The ECS array is in a different order, so match on the XP value.
        try:
            rows = self.experience_rows()
        except Exception:                                        # noqa: BLE001
            return members
        used: set[int] = set()
        for member in members:
            for slot, (current, total, _) in enumerate(rows):
                if slot in used:
                    continue
                if total == member.xp_total or current == member.xp_total:
                    member.xp_slot = slot
                    used.add(slot)
                    break

        # Match each member to their ClassesComponent row.  The save summary
        # lists the same classes in the same order, so comparing the class
        # names is enough to identify the row unambiguously.
        try:
            self._attach_classes(members)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            self._attach_progression(members)
        except Exception:                                        # noqa: BLE001
            pass
        return members

    def _attach_progression(self, members: list[PartyMember]) -> None:
        rows = self.progressions()
        taken: set[int] = set()
        for member in members:
            wanted = sorted((c.get("Main") or "").lower() for c in member.classes)
            if not wanted:
                continue
            for row in rows:
                if row["index"] in taken:
                    continue
                got = sorted(c.lower() for c in row["classes"])
                if got == wanted and len(row["levels"]) == member.level:
                    member.progression_row = row["index"]
                    member.feats = row["feats"]
                    taken.add(row["index"])
                    break

    def _attach_classes(self, members: list[PartyMember]) -> None:
        rows = self.class_rows()
        taken: set[int] = set()
        for member in members:
            wanted = [(c.get("Main") or "").lower() for c in member.classes]
            if not wanted:
                continue
            for row in rows:
                if row["index"] in taken or not row["classes"]:
                    continue
                got = [_main_class_name(e).lower() for e in row["classes"]]
                if sorted(got) == sorted(wanted) and row["total_level"] == member.level:
                    member.class_row = row["index"]
                    member.class_levels = row["classes"]
                    taken.add(row["index"])
                    # Stats and race are parallel arrays, so the same row index
                    # reads this character's abilities and race directly.
                    member.abilities = self.abilities(row["index"])
                    member.race_name = self.race_of(row["index"])
                    break

    def experience_rows(self) -> list[tuple[int, int, int]]:
        ecs = self.ecs()
        ct = ecs.get(EXPERIENCE)
        if ct is None:
            return []
        return [struct.unpack("<3i", ecs.element(ct, i)) for i in range(ct.count)]

    def set_experience(self, slot: int, total: int,
                       current_level: int | None = None) -> dict:
        """Patch one row of the experience component in place."""
        ecs = self.ecs()
        ct = ecs.get(EXPERIENCE)
        if ct is None:
            raise KeyError("this save has no experience component")
        if current_level is None:
            current_level = total
        ecs.write_field(ct, slot, 0, "<i", int(current_level))
        ecs.write_field(ct, slot, 4, "<i", int(total))
        self._ecs_dirty.add(GLOBALS_FILE)
        return {"slot": slot, "values": struct.unpack("<3i", ecs.element(ct, slot))}

    # ------------------------------------------------------------ classes
    def class_rows(self, filename: str = GLOBALS_FILE) -> list[dict]:
        """Decode every ClassesComponent element into named class levels."""
        ecs = self.ecs(filename)
        ct = ecs.get(CLASSES)
        if ct is None:
            return []
        data = gamedata.shared()
        rows: list[dict] = []
        for i in range(ct.count):
            ranges = ecs.element_ranges(ct, i)
            entries: list[dict] = []
            if ranges:
                blob = ecs.heap(*ranges[0])
                for off in range(0, len(blob) - CLASS_ENTRY.size + 1,
                                 CLASS_ENTRY.size):
                    raw_class, raw_sub, level, _ = CLASS_ENTRY.unpack_from(blob, off)
                    class_uuid = decode_uuid(raw_class)
                    sub_uuid = decode_uuid(raw_sub)
                    if class_uuid == NIL_UUID and sub_uuid == NIL_UUID:
                        continue
                    entries.append({
                        "class_uuid": class_uuid,
                        "subclass_uuid": "" if sub_uuid == NIL_UUID else sub_uuid,
                        "level": level,
                        "label": data.class_label(class_uuid, sub_uuid) or class_uuid,
                        "offset": ranges[0][0] + off,
                    })
            rows.append({
                "index": i,
                "classes": entries,
                "label": " / ".join(f"{e['label']} {e['level']}" for e in entries),
                "total_level": sum(e["level"] for e in entries),
            })
        return rows

    def set_class_level(self, row: int, entry: int, level: int,
                        filename: str = GLOBALS_FILE) -> dict:
        """Patch one class level in place inside the heap payload."""
        ecs = self.ecs(filename)
        ct = ecs.get(CLASSES)
        if ct is None:
            raise KeyError("this save has no classes component")
        ranges = ecs.element_ranges(ct, row)
        if not ranges:
            raise KeyError(f"class row {row} has no payload")
        begin, end = ranges[0]
        offset = begin + entry * CLASS_ENTRY.size + 32
        if offset + 4 > end:
            raise IndexError(f"class entry {entry} is outside row {row}")
        struct.pack_into("<I", ecs.arena, offset, int(level))
        self._ecs_dirty.add(filename)
        return {"row": row, "entry": entry, "level": int(level)}

    # ------------------------------------------- progression (feats, race)
    def progressions(self, filename: str = GLOBALS_FILE) -> list[dict]:
        """Each character's level-by-level history: class, subclass and feat.

        ``LevelUpComponent`` holds a heap list of pointers into the
        ``LevelUpComponentData`` array, which is what ties a run of level-ups to
        one character.
        """
        ecs = self.ecs(filename)
        owner = ecs.get(LEVELUP)
        data = ecs.get(LEVELUP_DATA)
        if owner is None or data is None:
            return []
        info = gamedata.shared()
        out: list[dict] = []
        for i in range(owner.count):
            ranges = ecs.element_ranges(owner, i)
            if not ranges:
                continue
            blob = ecs.heap(*ranges[0])
            levels: list[dict] = []
            for word_off in range(0, len(blob) - 7, 8):
                pointer = struct.unpack_from("<Q", blob, word_off)[0]
                rel = pointer - data.data_offset
                if rel < 0 or rel >= data.byte_length or rel % data.element_size:
                    continue
                row = rel // data.element_size
                raw = ecs.element(data, row)
                feat_uuid = decode_uuid(raw[FEAT_OFFSET:FEAT_OFFSET + 16])
                class_uuid = decode_uuid(raw[0:16])
                sub_uuid = decode_uuid(raw[16:32])
                levels.append({
                    "level": len(levels) + 1,
                    "data_row": row,
                    "class": info.name(class_uuid, ""),
                    "class_uuid": class_uuid,
                    "subclass": info.name(sub_uuid, "") if sub_uuid != NIL_UUID else "",
                    "feat": info.name(feat_uuid, "") if feat_uuid != NIL_UUID else "",
                    "feat_uuid": "" if feat_uuid == NIL_UUID else feat_uuid,
                })
            if levels:
                out.append({
                    "index": i,
                    "levels": levels,
                    "classes": list(dict.fromkeys(l["class"] for l in levels if l["class"])),
                    "feats": [l for l in levels if l["feat_uuid"]],
                })
        return out

    def set_feat(self, data_row: int, feat_uuid: str,
                 filename: str = GLOBALS_FILE) -> dict:
        """Replace the feat chosen at one level-up, in place."""
        ecs = self.ecs(filename)
        data = ecs.get(LEVELUP_DATA)
        if data is None:
            raise KeyError("this save has no level-up data")
        raw = encode_uuid(feat_uuid) if feat_uuid else b"\x00" * 16
        start = data.data_offset + data_row * data.element_size + FEAT_OFFSET
        ecs.arena[start:start + 16] = raw
        self._ecs_dirty.add(filename)
        info = gamedata.shared()
        return {"data_row": data_row, "feat": info.name(feat_uuid, feat_uuid)}

    def available_feats(self) -> list[dict]:
        return [{"uuid": d.uuid, "name": d.name}
                for d in gamedata.shared().of_category("feat")]

    # ---------------------------------------------------- ability scores
    def abilities(self, row: int, filename: str = GLOBALS_FILE) -> list[dict]:
        """The six ability scores for one character row.

        `StatsComponent`, `ClassesComponent` and `RaceComponent` all have one
        element per character in the same order, so a row index matched through
        any of them reads across all three.
        """
        ecs = self.ecs(filename)
        ct = ecs.get(STATS)
        if ct is None or not 0 <= row < ct.count:
            return []
        values = struct.unpack_from("<6i", ecs.element(ct, row), ABILITY_OFFSET)
        return [{"name": ABILITY_NAMES[i], "short": ABILITY_SHORT[i],
                 "value": values[i], "index": i}
                for i in range(6)]

    def set_ability(self, row: int, index: int, value: int,
                    filename: str = GLOBALS_FILE) -> dict:
        """Patch one ability score in place."""
        if not 0 <= index < 6:
            raise IndexError(f"ability index {index} out of range")
        ecs = self.ecs(filename)
        ct = ecs.get(STATS)
        if ct is None:
            raise KeyError("this save has no stats component")
        ecs.write_field(ct, row, ABILITY_OFFSET + index * 4, "<i", int(value))
        self._ecs_dirty.add(filename)
        return {"row": row, "ability": ABILITY_NAMES[index], "value": int(value)}

    def race_of(self, row: int, filename: str = GLOBALS_FILE) -> str:
        ecs = self.ecs(filename)
        ct = ecs.get(RACE)
        if ct is None or not 0 <= row < ct.count:
            return ""
        uuid = decode_uuid(ecs.element(ct, row)[:16])
        if uuid == NIL_UUID:
            return ""
        return gamedata.shared().name(uuid, uuid)

    def race_rows(self, filename: str = GLOBALS_FILE) -> list[dict]:
        ecs = self.ecs(filename)
        ct = ecs.get(RACE)
        if ct is None:
            return []
        info = gamedata.shared()
        rows = []
        for i in range(ct.count):
            uuid = decode_uuid(ecs.element(ct, i)[:16])
            if uuid == NIL_UUID:
                continue
            rows.append({"index": i, "uuid": uuid, "name": info.name(uuid, uuid)})
        return rows

    # -------------------------------------------------------------- items
    def items(self, filename: str = GLOBALS_FILE) -> dict:
        """Every item in the save, named via the game's root templates.

        `Items/ItemFactory/Creators` pairs an entity UUID with the root template
        it was created from, which is what turns a save full of GUIDs into a
        readable inventory listing.
        """
        doc = self.lsf_document(filename)
        factory = doc.resource.regions.get("Items")
        if factory is None:
            return {"items": [], "resolved": 0, "total": 0}
        creators_node = factory.child("ItemFactory")
        creators = (creators_node.child("Creators").child_list("Creator")
                    if creators_node and creators_node.child("Creators") else [])

        templates = gamedata.root_templates()
        counts: dict[str, dict] = {}
        resolved = 0
        for creator in creators:
            template_id = str(creator.get("TemplateID") or "").lower()
            entry = templates.get(template_id)
            if entry is None:
                continue
            resolved += 1
            row = counts.setdefault(template_id, {
                "template": template_id, "name": entry.name,
                "type": entry.type, "stats": entry.stats,
                "count": 0, "entities": [],
            })
            row["count"] += 1
            if len(row["entities"]) < 12:
                row["entities"].append(str(creator.get("Entity") or ""))

        items = sorted(counts.values(), key=lambda r: (-r["count"], r["name"]))
        return {"items": items, "resolved": resolved, "total": len(creators),
                "templates_known": len(templates)}

    def entity_index(self, uuid: str, filename: str = GLOBALS_FILE) -> int | None:
        """Entity index for a UUID taken from the resource tree."""
        return self.ecs(filename).index_for_uuid(uuid)

    # -------------------------------------------- generic component access
    def component_types(self, filename: str = GLOBALS_FILE,
                        query: str = "", populated_only: bool = True) -> list[dict]:
        ecs = self.ecs(filename)
        types = ecs.types
        if populated_only:
            types = [t for t in types if t.count]
        if query:
            low = query.lower()
            types = [t for t in types if low in t.name.lower()]
        return [t.to_dict() for t in types]

    def component_rows(self, filename: str, name: str,
                       start: int = 0, limit: int = 50) -> dict:
        """Raw element bytes, plus a few plausible numeric readings."""
        ecs = self.ecs(filename)
        ct = ecs.get(name)
        if ct is None:
            raise KeyError(name)
        rows = []
        for i in range(start, min(start + limit, ct.count)):
            raw = ecs.element(ct, i)
            row = {
                "index": i,
                "hex": raw.hex(" "),
                "int32": _safe_unpack(raw, "<i"),
                "uint32": _safe_unpack(raw, "<I"),
                "int64": _safe_unpack(raw, "<q"),
                "float": _safe_unpack(raw, "<f"),
            }
            # Variable-length payloads are referenced as heap ranges; show a
            # preview and any entity the payload points at.
            ranges = ecs.element_ranges(ct, i)
            if ranges:
                row["heap"] = "; ".join(
                    f"{end - begin}B @{begin}" for begin, end in ranges)
                preview = ecs.heap(*ranges[0])[:24]
                row["heap_hex"] = preview.hex(" ")
                entities = ecs.referenced_entities(ct, i)
                if entities:
                    row["entities"] = entities[:8]
            rows.append(row)
        return {"type": ct.to_dict(), "rows": rows, "total": ct.count,
                "has_heap": any("heap" in r for r in rows)}

    def set_component_field(self, filename: str, name: str, index: int,
                            offset: int, kind: str, value: float) -> dict:
        fmt = FIELD_FORMATS.get(kind)
        if fmt is None:
            raise ValueError(f"unknown field type {kind!r}")
        ecs = self.ecs(filename)
        ct = ecs.get(name)
        if ct is None:
            raise KeyError(name)
        native = float(value) if kind in ("float", "double") else int(float(value))
        ecs.write_field(ct, index, offset, fmt, native)
        self._ecs_dirty.add(filename)
        return {"hex": ecs.element(ct, index).hex(" ")}

    # ------------------------------------------------------- raw LSF tree
    def tree_children(self, filename: str, path: str = "") -> list[dict]:
        """One level of the resource tree, for lazy expansion in the UI."""
        doc = self.lsf_document(filename)
        out: list[dict] = []
        if not path:
            for name, region in doc.resource.regions.items():
                out.append(_node_summary(region, name))
            return out
        node = self._resolve(doc, path)
        for cname, kids in node.children.items():
            for i, child in enumerate(kids):
                out.append(_node_summary(child, f"{path}/{cname}[{i}]"))
        return out

    def node_detail(self, filename: str, path: str) -> dict:
        doc = self.lsf_document(filename)
        node = self._resolve(doc, path)
        return {
            "path": path,
            "name": node.name,
            "attributes": [
                {"name": a.name, "type": a.type_name, "type_id": int(a.type),
                 "value": _display(a.value), "editable": _is_editable(a.type)}
                for a in node.attributes.values()
            ],
            "children": [{"name": k, "count": len(v)} for k, v in node.children.items()],
        }

    def set_attribute(self, filename: str, path: str, attr: str, value: Any) -> dict:
        doc = self.lsf_document(filename)
        node = self._resolve(doc, path)
        existing = node.attributes.get(attr)
        if existing is None:
            raise KeyError(f"{path} has no attribute {attr!r}")
        node.set(attr, _coerce(existing.type, value))
        self._touch_lsf(filename)
        return {"name": attr, "value": _display(node.get(attr))}

    def search(self, filename: str, needle: str, limit: int = 200) -> list[dict]:
        """Find nodes or attributes matching a string."""
        doc = self.lsf_document(filename)
        low = needle.lower()
        hits: list[dict] = []
        for region_name, region in doc.resource.regions.items():
            for node, path in _walk_paths(region, region_name):
                if low in node.name.lower():
                    hits.append({"path": path, "name": node.name, "match": "node"})
                    if len(hits) >= limit:
                        return hits
                for a in node.attributes.values():
                    if low in a.name.lower() or low in str(a.value).lower():
                        hits.append({"path": path, "name": node.name,
                                     "match": f"{a.name} = {_display(a.value)}"[:120]})
                        if len(hits) >= limit:
                            return hits
        return hits

    @staticmethod
    def _resolve(doc: lsf.LSFDocument, path: str) -> Node:
        parts = path.split("/")
        head = parts[0]
        node = doc.resource.regions.get(head)
        if node is None:
            raise KeyError(f"no region {head!r}")
        for part in parts[1:]:
            name, _, idx = part.partition("[")
            i = int(idx.rstrip("]")) if idx else 0
            kids = node.children.get(name)
            if not kids or i >= len(kids):
                raise KeyError(f"no child {part!r} under {node.name!r}")
            node = kids[i]
        return node

    # ------------------------------------------------------------- saving
    def _touch_lsf(self, filename: str) -> None:
        self._dirty_lsf.add(filename)
        if filename == GLOBALS_FILE:
            self.save.touch(GLOBALS_FILE)

    @property
    def dirty(self) -> bool:
        return self.save.dirty or bool(self._ecs_dirty) or bool(self._dirty_lsf)

    def flush(self) -> None:
        """Push ECS edits into their LSF nodes, then every edited LSF into the package."""
        for filename in sorted(self._ecs_dirty):
            doc = self.lsf_document(filename)
            region = doc.resource.regions.get("NewAge")
            if region is not None and filename in self._ecs:
                region.set("NewAge", self._ecs[filename].to_bytes())
            self._dirty_lsf.add(filename)
        self._ecs_dirty.clear()

        for filename in sorted(self._dirty_lsf):
            if filename == GLOBALS_FILE:
                # Globals is re-serialised by Savegame itself.
                self.save.touch(GLOBALS_FILE)
            else:
                self.save.package[filename].data = self._docs[filename].to_bytes()
        self._dirty_lsf.clear()

    def write(self, path: str | None = None, backup: bool = True) -> dict:
        """Write the savegame back to disk.

        Note that Baldur's Gate 3 will **not load** a save whose entity-component
        data has been edited - abilities, classes, feats and experience all live
        there.  The resulting file is structurally valid and re-reads correctly,
        but the game rejects it.  See "The write problem" in the README.  The
        returned dict reports whether any ECS edit was included so callers can
        warn.
        """
        touched_ecs = bool(self._ecs_dirty)
        self.flush()
        result = self.save.save(path, backup=backup)
        result["ecs_modified"] = touched_ecs
        result["game_will_load"] = not touched_ecs
        return result


# ------------------------------------------------------------------ helpers

def _main_class_name(entry: dict) -> str:
    """"Fighter (Champion)" -> "Fighter"."""
    return (entry.get("label") or "").split(" (", 1)[0].strip()


def _safe_unpack(raw: bytes, fmt: str):
    size = struct.calcsize(fmt)
    if len(raw) < size:
        return None
    value = struct.unpack_from(fmt, raw, 0)[0]
    if fmt == "<f":
        return None if value != value or abs(value) > 1e30 else round(value, 4)
    return value


def _node_summary(node: Node, path: str) -> dict:
    return {
        "path": path,
        "name": node.name,
        "attributes": len(node.attributes),
        "children": sum(len(v) for v in node.children.values()),
    }


def _walk_paths(node: Node, path: str) -> Iterator[tuple[Node, str]]:
    yield node, path
    for cname, kids in node.children.items():
        for i, child in enumerate(kids):
            yield from _walk_paths(child, f"{path}/{cname}[{i}]")


def _is_editable(type_id: DataType) -> bool:
    from .formats.resource import NUMERIC_TYPES, STRING_TYPES
    return type_id in NUMERIC_TYPES or type_id in STRING_TYPES or \
        type_id in (DataType.BOOL, DataType.UUID)


def _display(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _coerce(type_id: DataType, value: Any) -> Any:
    from .formats.resource import NUMERIC_TYPES, STRING_TYPES
    if type_id == DataType.BOOL:
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if type_id in (DataType.FLOAT, DataType.DOUBLE):
        return float(value)
    if type_id in NUMERIC_TYPES:
        return int(float(value))
    if type_id in STRING_TYPES or type_id == DataType.UUID:
        return str(value)
    raise ValueError(f"cannot edit attributes of type {type_id.name}")
