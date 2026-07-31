"""Reader/writer for LSMF - the Baldur's Gate 3 ECS blob ("NewAge").

Every savegame stores its entity-component data in a SCRATCHBUFFER attribute
of the ``NewAge`` region.  That buffer is a self-contained container:

    header (48 bytes)
        0x00  char[4]  'LSMF'
        0x04  uint8    major, uint8 minor, uint16 flags
        0x08  uint64   content hash
        0x10  uint64   section A size (the component data arena)
        0x18  uint64   section B size (the type table)
        0x20  uint32   length of the name blob at the start of section B
        0x24  uint16   number of component types
        0x26  uint16   unknown, always 32 in observed saves
        0x28  uint64   reserved
    section A   component arrays, laid end to end with alignment padding
    section B   [name blob][type records]

Each 48-byte type record is::

    0x00 uint64 name offset      into the name blob
    0x08 uint32 name length
    0x0C uint32 entity count     (total live entities in this file)
    0x10 uint64 type hash
    0x18 uint32 element size
    0x1C uint32 component version   - always matches the ".vN." in the name
    0x20 uint64 element count
    0x28 uint64 data offset      into section A

Edits are applied **in place**: a component element is overwritten byte for
byte and nothing moves.  That keeps every offset in the arena valid, which is
what makes writing to a 6 MB ECS blob safe without re-serialising it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"LSMF"
HEADER = struct.Struct("<4sBBHQQQIHHQ")
RECORD = struct.Struct("<QIIQIIQQ")

assert HEADER.size == 48
assert RECORD.size == 48


class LSMFError(Exception):
    pass


@dataclass
class ComponentType:
    """One component array inside the arena."""

    index: int
    name: str
    version: int
    type_hash: int
    element_size: int
    count: int
    data_offset: int
    entity_count: int
    name_offset: int

    @property
    def byte_length(self) -> int:
        return self.count * self.element_size

    @property
    def short_name(self) -> str:
        """`game.experience.v0.ExperienceComponent` -> `ExperienceComponent`."""
        return self.name.rsplit(".", 1)[-1]

    def to_dict(self) -> dict:
        return {
            "index": self.index, "name": self.name, "short_name": self.short_name,
            "version": self.version, "element_size": self.element_size,
            "count": self.count, "data_offset": self.data_offset,
            "bytes": self.byte_length,
        }


class LSMFDocument:
    """A parsed ECS blob that supports in-place element edits."""

    def __init__(self) -> None:
        self._header = b""
        self.arena = bytearray()
        self.type_table = b""
        self.name_blob = b""
        self.types: list[ComponentType] = []
        self._by_name: dict[str, ComponentType] = {}
        #: End of the fixed-size component arrays; everything after is heap.
        self.data_end = 0
        self._uuid_map: dict[str, list[int]] | None = None

    # ------------------------------------------------------------ parsing
    @classmethod
    def from_bytes(cls, blob: bytes) -> "LSMFDocument":
        if len(blob) < HEADER.size or blob[:4] != MAGIC:
            raise LSMFError("not an LSMF blob")
        (_, _major, _minor, _flags, _hash, size_a, size_b,
         names_len, n_types, _unknown, _reserved) = HEADER.unpack_from(blob, 0)

        if HEADER.size + size_a + size_b != len(blob):
            raise LSMFError(
                f"section sizes {size_a}+{size_b} do not fill {len(blob)} bytes"
            )

        doc = cls()
        doc._header = bytes(blob[:HEADER.size])
        doc.arena = bytearray(blob[HEADER.size:HEADER.size + size_a])
        section_b = blob[HEADER.size + size_a:HEADER.size + size_a + size_b]
        doc.name_blob = bytes(section_b[:names_len])
        doc.type_table = bytes(section_b[names_len:])

        if len(doc.type_table) < n_types * RECORD.size:
            raise LSMFError("type table is shorter than the declared type count")

        for i in range(n_types):
            (name_off, name_len, entity_count, type_hash,
             elem, version, count, data_off) = RECORD.unpack_from(
                doc.type_table, i * RECORD.size)
            name = doc.name_blob[name_off:name_off + name_len].decode(
                "ascii", "replace")
            if data_off + count * elem > size_a:
                raise LSMFError(
                    f"component {name!r} runs past the arena "
                    f"({data_off}+{count}*{elem} > {size_a})"
                )
            ct = ComponentType(index=i, name=name, version=version,
                               type_hash=type_hash, element_size=elem,
                               count=count, data_offset=data_off,
                               entity_count=entity_count, name_offset=name_off)
            doc.types.append(ct)
            doc._by_name.setdefault(name, ct)
        doc.data_end = max((t.data_offset + t.byte_length for t in doc.types),
                           default=0)
        return doc

    # --------------------------------------------------------------- heap
    # Variable-length component data (vectors, owner lists) lives in the
    # region after the fixed arrays.  An element references it as a pair of
    # absolute (begin, end) arena offsets, which is how a serialised vector
    # is laid out.  Consecutive elements chain: element[i].end == element[i+1].begin.

    def is_heap_range(self, begin: int, end: int) -> bool:
        return self.data_end <= begin <= end <= len(self.arena)

    def element_ranges(self, ct: ComponentType, index: int) -> list[tuple[int, int]]:
        """The (begin, end) heap ranges an element points at, if any."""
        if ct.element_size < 16 or ct.element_size % 8:
            return []
        raw = self.element(ct, index)
        words = struct.unpack(f"<{ct.element_size // 8}Q", raw)
        out = []
        for i in range(len(words) // 2):
            begin, end = words[i * 2], words[i * 2 + 1]
            if self.is_heap_range(begin, end):
                out.append((begin, end))
        return out

    def heap(self, begin: int, end: int) -> bytes:
        if not self.is_heap_range(begin, end):
            raise LSMFError(f"[{begin}, {end}) is not inside the heap")
        return bytes(self.arena[begin:end])

    def entity_index(self, offset: int) -> int | None:
        """Turn a byte offset into the EntityId array into an entity index."""
        eid = self.get("core.v0.EntityId")
        if eid is None or not eid.element_size:
            return None
        rel = offset - eid.data_offset
        if rel < 0 or rel % eid.element_size or rel // eid.element_size >= eid.count:
            return None
        return rel // eid.element_size

    # --------------------------------------------------------- entity ids
    # `core.v0.EntityId` is an array of 16-byte GUIDs in Larian's
    # little-endian-groups byte order.  It is a *reference* array, not a
    # registry: heap payloads point into it by byte offset, and the same entity
    # legitimately appears in many slots (one save has 9,374 slots holding 1,957
    # distinct GUIDs, one of them repeated 99 times).  Slot -> GUID is therefore
    # exact, while GUID -> slot is one-to-many.

    def entity_uuid(self, index: int) -> str | None:
        from .lsf import decode_uuid                # local: avoids a cycle

        eid = self.get("core.v0.EntityId")
        if eid is None or not 0 <= index < eid.count:
            return None
        return decode_uuid(self.element(eid, index)[:16])

    def entity_uuid_map(self) -> dict[str, list[int]]:
        """UUID (lowercase) -> every slot holding it, built once and cached."""
        if self._uuid_map is None:
            from .lsf import decode_uuid

            eid = self.get("core.v0.EntityId")
            mapping: dict[str, list[int]] = {}
            if eid is not None:
                for i in range(eid.count):
                    key = decode_uuid(self.element(eid, i)[:16]).lower()
                    mapping.setdefault(key, []).append(i)
            self._uuid_map = mapping
        return self._uuid_map

    def indices_for_uuid(self, uuid: str) -> list[int]:
        return list(self.entity_uuid_map().get((uuid or "").lower(), ()))

    def index_for_uuid(self, uuid: str) -> int | None:
        """First slot holding this entity, or None."""
        slots = self.entity_uuid_map().get((uuid or "").lower())
        return slots[0] if slots else None

    def referenced_entities(self, ct: ComponentType, index: int) -> list[int]:
        """Entity indices referenced by an element's heap ranges."""
        found: list[int] = []
        for begin, end in self.element_ranges(ct, index):
            blob = self.heap(begin, end)
            for off in range(0, len(blob) - 7, 8):
                value = struct.unpack_from("<Q", blob, off)[0]
                entity = self.entity_index(value)
                if entity is not None:
                    found.append(entity)
        return found

    # ------------------------------------------------------------- access
    def get(self, name: str) -> ComponentType | None:
        """Look up by full name, or by the trailing short name."""
        ct = self._by_name.get(name)
        if ct is not None:
            return ct
        matches = [t for t in self.types if t.short_name == name]
        return matches[0] if len(matches) == 1 else None

    def find(self, needle: str) -> list[ComponentType]:
        low = needle.lower()
        return [t for t in self.types if low in t.name.lower()]

    def element(self, ct: ComponentType, index: int) -> bytes:
        if not 0 <= index < ct.count:
            raise IndexError(f"{ct.name}: index {index} of {ct.count}")
        start = ct.data_offset + index * ct.element_size
        return bytes(self.arena[start:start + ct.element_size])

    def elements(self, ct: ComponentType) -> list[bytes]:
        return [self.element(ct, i) for i in range(ct.count)]

    # ------------------------------------------------------------ editing
    def write_element(self, ct: ComponentType, index: int, data: bytes) -> None:
        """Overwrite one element in place; the size must match exactly."""
        if len(data) != ct.element_size:
            raise LSMFError(
                f"{ct.name}: element is {ct.element_size} bytes, got {len(data)}"
            )
        if not 0 <= index < ct.count:
            raise IndexError(f"{ct.name}: index {index} of {ct.count}")
        start = ct.data_offset + index * ct.element_size
        self.arena[start:start + ct.element_size] = data

    def write_field(self, ct: ComponentType, index: int, offset: int,
                    fmt: str, *values) -> None:
        """Patch a single packed field inside one element."""
        size = struct.calcsize(fmt)
        if offset + size > ct.element_size:
            raise LSMFError(
                f"{ct.name}: field at {offset}+{size} exceeds element size "
                f"{ct.element_size}"
            )
        start = ct.data_offset + index * ct.element_size + offset
        struct.pack_into(fmt, self.arena, start, *values)

    def read_field(self, ct: ComponentType, index: int, offset: int, fmt: str):
        start = ct.data_offset + index * ct.element_size + offset
        return struct.unpack_from(fmt, self.arena, start)

    # ------------------------------------------------------------ writing
    def to_bytes(self) -> bytes:
        """Reassemble the blob.  Only arena bytes can have changed."""
        return self._header + bytes(self.arena) + self.name_blob + self.type_table

    def summary(self) -> dict:
        return {
            "types": len(self.types),
            "arena_bytes": len(self.arena),
            "populated": sum(1 for t in self.types if t.count),
        }

    def __repr__(self) -> str:
        return f"<LSMFDocument types={len(self.types)} arena={len(self.arena):,}>"


def loads(blob: bytes) -> LSMFDocument:
    return LSMFDocument.from_bytes(blob)
