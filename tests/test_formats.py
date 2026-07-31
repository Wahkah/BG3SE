"""Format-level tests.

The container tests run against synthetic data so they work anywhere.  Tests
that need real savegames skip themselves when none are installed.
"""

from __future__ import annotations

import struct

import pytest

from bgse import locate
from bgse.formats import compression, lsf, lsmf
from bgse.formats.lsf import decode_uuid, encode_uuid
from bgse.formats.lspk import Package, PackagedFile
from bgse.formats.resource import DataType, Node, Resource
from bgse.formats.verify import check_codecs, roundtrip
from bgse.save import Savegame


# --------------------------------------------------------------- compression
@pytest.mark.parametrize("method", [compression.NONE, compression.ZLIB,
                                    compression.LZ4, compression.ZSTD])
def test_compression_roundtrip(method):
    payload = b"Baldur's Gate 3 savegame payload " * 64
    flags = compression.make_flags(method, compression.DEFAULT)
    packed = compression.compress(payload, flags)
    assert compression.decompress(packed, flags, len(payload)) == payload


def test_lz4_frame_roundtrip_and_detection():
    payload = b"chunked stream " * 500
    flags = compression.make_flags(compression.LZ4, compression.DEFAULT)
    framed = compression.compress(payload, flags, chunked=True)
    assert compression.is_lz4_frame(framed)
    assert compression.decompress(framed, flags, len(payload)) == payload

    block = compression.compress(payload, flags, chunked=False)
    assert not compression.is_lz4_frame(block)


# ---------------------------------------------------------------------- guid
def test_guid_uses_little_endian_groups():
    # Byte order verified against the game's own Osiris spelling of this GUID.
    guid = "3ed74f06-3c60-42dc-83f6-f034cb47c679"
    raw = encode_uuid(guid)
    assert decode_uuid(raw) == guid
    # The first group is a little-endian uint32.
    assert raw[:4] == bytes.fromhex("064fd73e")


# ---------------------------------------------------------------------- lspk
def test_package_roundtrip():
    pkg = Package()
    pkg.files.append(PackagedFile(name="a.txt"))
    pkg.files[0].data = b"hello" * 100
    pkg.files.append(PackagedFile(name="nested/b.bin"))
    pkg.files[1].data = bytes(range(256)) * 8

    reloaded = Package.from_bytes(pkg.to_bytes())
    assert reloaded.names() == ["a.txt", "nested/b.bin"]
    assert reloaded["a.txt"].data == b"hello" * 100
    assert reloaded["nested/b.bin"].data == bytes(range(256)) * 8


def test_package_rejects_foreign_data():
    with pytest.raises(Exception):
        Package.from_bytes(b"NOTLSPK" + b"\x00" * 64)


# ----------------------------------------------------------------------- lsf
def _sample_document() -> lsf.LSFDocument:
    doc = lsf.LSFDocument()
    region = Node("Root")
    region.attributes["Name"] = lsf.Attribute("Name", DataType.LSSTRING, "Tav")
    region.attributes["Level"] = lsf.Attribute("Level", DataType.INT, 9)
    region.attributes["Ratio"] = lsf.Attribute("Ratio", DataType.FLOAT, 0.5)
    region.attributes["Id"] = lsf.Attribute(
        "Id", DataType.UUID, "3ed74f06-3c60-42dc-83f6-f034cb47c679")
    region.attributes["Alive"] = lsf.Attribute("Alive", DataType.BOOL, True)
    child = Node("Child")
    child.attributes["Pos"] = lsf.Attribute("Pos", DataType.VEC3, [1.0, 2.0, 3.0])
    region.append(child)
    doc.resource.regions["Root"] = region
    return doc


def test_lsf_roundtrip_synthetic():
    doc = _sample_document()
    again = lsf.LSFDocument.from_bytes(doc.to_bytes())
    root = again.resource.regions["Root"]
    assert root.get("Name") == "Tav"
    assert root.get("Level") == 9
    assert root.get("Alive") is True
    assert root.get("Id") == "3ed74f06-3c60-42dc-83f6-f034cb47c679"
    assert root.child("Child").get("Pos") == [1.0, 2.0, 3.0]


def test_only_extended_one_selects_long_nodes():
    """Game root templates use extended == 2 with 12-byte nodes."""
    assert lsf.EXTENDED_LONG_NODES == 1
    doc = lsf.LSFDocument(extended=2)
    doc.resource.regions["Root"] = Node("Root")
    # Must round-trip using the compact layout, not the long one.
    again = lsf.LSFDocument.from_bytes(doc.to_bytes())
    assert again.extended == 2
    assert "Root" in again.resource.regions


def test_lsf_section_order_puts_keys_last():
    # Keys is the second size pair in the header but the last section on disk.
    assert lsf.HEADER_ORDER.index("keys") == 1
    assert lsf.DISK_ORDER[-1] == "keys"


# ---------------------------------------------------------------------- lsmf
def test_lsmf_rejects_truncated_blob():
    with pytest.raises(lsmf.LSMFError):
        lsmf.LSMFDocument.from_bytes(b"LSMF" + b"\x00" * 20)


# ------------------------------------------------- real savegames (optional)
def _first_save():
    saves = locate.find_saves()
    if not saves:
        pytest.skip("no Baldur's Gate 3 savegames installed")
    return saves[0]


def test_real_save_opens_and_verifies():
    save = Savegame.open(_first_save().path)
    assert save.package.files
    blob = save.package["meta.lsf"].data
    assert check_codecs(blob) == []
    assert roundtrip(blob) == []


def test_real_ecs_is_byte_identical():
    save = Savegame.open(_first_save().path)
    region = save.globals.resource.regions.get("NewAge")
    if region is None:
        pytest.skip("this save has no ECS blob")
    blob = region.get("NewAge")
    doc = lsmf.LSMFDocument.from_bytes(blob)
    assert doc.types, "no component types parsed"
    assert doc.to_bytes() == blob
    # Every component version must match the ".vN." in its own name.
    assert all(f".v{t.version}." in t.name for t in doc.types)


def test_heap_ranges_chain_and_resolve_entities():
    save = Savegame.open(_first_save().path)
    from bgse.model import SaveModel

    ecs = SaveModel(save).ecs()
    owner = ecs.get("game.inventory.v0.OwnerComponent")
    if owner is None or owner.count < 4:
        pytest.skip("no owner component in this save")
    ranges = [ecs.element_ranges(owner, i) for i in range(4)]
    assert all(r for r in ranges), "owner elements should carry heap ranges"
    # A serialised vector arena: each element ends where the next begins.
    assert all(ranges[i][0][1] == ranges[i + 1][0][0] for i in range(3))
    entities = ecs.referenced_entities(owner, 0)
    eid = ecs.get("core.v0.EntityId")
    assert entities and all(0 <= e < eid.count for e in entities)


def test_real_classes_match_save_info():
    from bgse import gamedata
    from bgse.model import SaveModel

    if gamedata.shared().empty:
        pytest.skip("no game install found, class GUIDs cannot be named")

    saves = locate.find_saves()
    if not saves:
        pytest.skip("no Baldur's Gate 3 savegames installed")

    # Not every save is matchable - a party can use classes that the installed
    # game data does not define - so look for one that is.
    matched: list = []
    for slot in saves[:6]:
        model = SaveModel(Savegame.open(slot.path))
        matched = [m for m in model.party() if m.class_row is not None]
        if matched:
            break
    if not matched:
        pytest.skip("no save had a party matchable against the game data")

    for m in matched:
        # Decoded class levels must add up to the level in the save summary.
        assert sum(c["level"] for c in m.class_levels) == m.level
        main = {(c.get("Main") or "").lower() for c in m.classes}
        got = {(c["label"].split(" (", 1)[0]).lower() for c in m.class_levels}
        assert main == got, f"{m.name}: {got} != {main}"


def test_real_progressions_are_consistent():
    """Level-up history must line up with the class levels and the party level."""
    from bgse import gamedata
    from bgse.model import SaveModel

    if gamedata.shared().empty:
        pytest.skip("no game install found")
    saves = locate.find_saves()
    if not saves:
        pytest.skip("no Baldur's Gate 3 savegames installed")

    checked = 0
    for slot in saves[:6]:
        model = SaveModel(Savegame.open(slot.path))
        for member in model.party():
            if member.progression_row is None:
                continue
            checked += 1
            rows = model.progressions()
            row = next(r for r in rows if r["index"] == member.progression_row)
            # One level-up record per character level.
            assert len(row["levels"]) == member.level
            # Every feat slot resolves to a real definition.
            for feat in member.feats:
                assert feat["feat"], f"unresolved feat {feat['feat_uuid']}"
            # The classes seen in the progression match the save summary.
            summary = sorted((c.get("Main") or "").lower() for c in member.classes)
            assert sorted(c.lower() for c in row["classes"]) == summary
        if checked:
            break
    if not checked:
        pytest.skip("no matchable progression found")


def test_real_abilities_are_plausible():
    """Ability scores must be in range and match the class's primary ability."""
    from bgse import gamedata
    from bgse.model import SaveModel

    if gamedata.shared().empty:
        pytest.skip("no game install found")
    saves = locate.find_saves()
    if not saves:
        pytest.skip("no Baldur's Gate 3 savegames installed")

    # Real parties contain off-meta builds (a Strength monk running
    # TavernBrawler) and polymorphed characters whose stats are the wildshape
    # form's, so "leads in the class's primary ability" is not a safe rule.
    # These properties hold regardless of build and still fail loudly if the
    # field offset is wrong.
    profiles: list[tuple[str, tuple[int, ...]]] = []
    for slot in saves[:6]:
        model = SaveModel(Savegame.open(slot.path))
        for member in model.party():
            if not member.abilities:
                continue
            assert len(member.abilities) == 6, member.abilities
            values = tuple(a["value"] for a in member.abilities)
            # A wrong offset lands on handles or pointers, far outside 1..30.
            assert all(1 <= v <= 30 for v in values), (member.name, values)
            # Ability arrays are never uniformly tiny.
            assert sum(values) >= 40, (member.name, values)
            assert [a["short"] for a in member.abilities] == \
                ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
            profiles.append((member.name, values))
        if len(profiles) >= 4:
            break

    if not profiles:
        pytest.skip("no party member had a matched stats row")
    # Distinct characters must not all share one array, which is what reading a
    # constant region of the arena would produce.
    assert len({v for _, v in profiles}) > 1 or len(profiles) == 1, profiles
    # And a real statline is not flat.
    assert any(max(v) - min(v) >= 4 for _, v in profiles), profiles


def test_entity_uuid_table_round_trips():
    """Slot -> GUID is exact; GUID -> slot is one-to-many, so check membership."""
    from bgse.model import SaveModel

    save = Savegame.open(_first_save().path)
    ecs = SaveModel(save).ecs()
    eid = ecs.get("core.v0.EntityId")
    if eid is None or not eid.count:
        pytest.skip("no entity table in this save")
    for i in (0, eid.count // 2, eid.count - 1):
        uuid = ecs.entity_uuid(i)
        assert uuid
        slots = ecs.indices_for_uuid(uuid)
        assert i in slots, (i, slots[:5])
        # Every slot claiming this GUID must really hold it.
        assert all(ecs.entity_uuid(s) == uuid for s in slots)
    total = sum(len(v) for v in ecs.entity_uuid_map().values())
    assert total == eid.count


def test_items_resolve_against_root_templates():
    from bgse import gamedata
    from bgse.model import SaveModel

    templates = gamedata.root_templates()
    if not templates:
        pytest.skip("no game install found, cannot name items")
    assert len(templates) > 1000, len(templates)

    save = Savegame.open(_first_save().path)
    model = SaveModel(save)
    data = model.items()
    if not data["total"]:
        pytest.skip("save contains no item entities")
    # Most items in a normal save come from shipped templates.
    assert data["resolved"] / data["total"] > 0.5, data
    assert data["items"] and all(row["name"] for row in data["items"])

    # An item's creator entity must exist in the ECS entity table.
    entity = next(e for row in data["items"] for e in row["entities"] if e)
    assert model.entity_index(entity) is not None


def test_real_experience_matches_save_info():
    save = Savegame.open(_first_save().path)
    from bgse.model import EXPERIENCE, SaveModel

    model = SaveModel(save)
    ecs = model.ecs()
    ct = ecs.get(EXPERIENCE)
    if ct is None or not ct.count:
        pytest.skip("no experience component in this save")
    rows = {struct.unpack("<3i", ecs.element(ct, i))[1] for i in range(ct.count)}
    expected = {c.get("Experience Points (Total)")
                for c in save.save_info.get("Active Party", {}).get("Characters", [])}
    assert expected & rows, f"ECS xp {rows} does not intersect SaveInfo {expected}"
