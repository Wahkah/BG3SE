"""Reader/writer for Larian LSF binary resource files (versions 6 and 7).

Header (64 bytes)::

    0x00  char[4]  'LSOF'
    0x04  uint32   version (6 or 7)
    0x08  uint64   engine version (packed major/minor/revision/build)
    0x10  uint32[10]  (uncompressed, on-disk) size pairs for the five sections:
                      strings, keys, nodes, attributes, values
    0x38  uint8    compression flags (0x22 = LZ4, default level)
    0x39  uint8[3] unknown, preserved verbatim
    0x3C  uint32   "extended" flag; only the value 1 selects the long layout
    0x40           section data, in header order

In the compact layout nodes and attributes are 12 bytes each, attributes are
stored in file order tagged with their owning node, and each value directly
follows the previous one in the values section.  Only when ``extended == 1`` do
both structs grow to 16 bytes and carry explicit sibling links and value
offsets.  Other non-zero values still mean the compact layout - the game's own
root template files use ``extended == 2`` with 12-byte nodes, so testing for
"non-zero" here silently misreads them.

The original string hash table is preserved when rewriting, so every existing
name reference keeps resolving to exactly the same bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from . import compression
from .resource import (
    Attribute, DataType, Node, Resource, ResourceMetadata,
    TranslatedFSString, TranslatedFSStringArgument, TranslatedString,
)

MAGIC = b"LSOF"
HEADER = struct.Struct("<4sIQ10I4BI")
assert HEADER.size == 64

NODE_V2 = struct.Struct("<Iii")      # name ref, first attribute, parent
NODE_V3 = struct.Struct("<Iiii")     # name ref, parent, next sibling, first attribute
ATTR_V2 = struct.Struct("<IIi")      # name ref, type+length, node index
ATTR_V3 = struct.Struct("<IIiI")     # name ref, type+length, next attribute, offset
KEY_ENTRY = struct.Struct("<II")     # node index, key name ref

SUPPORTED_VERSIONS = (6, 7)

#: Order of the size pairs in the header.
HEADER_ORDER = ("strings", "keys", "nodes", "attributes", "values")

#: Order the sections actually appear in on disk.  Note that keys is declared
#: second in the header but written last; this is only observable in files that
#: have a non-empty keys section (LSF v6 level caches).
DISK_ORDER = ("strings", "nodes", "attributes", "values", "keys")

#: Larian frames every substream as LZ4 except the string table.
DEFAULT_CHUNKED = {"strings": False, "keys": True, "nodes": True,
                   "attributes": True, "values": True}

#: The only "extended" value that selects 16-byte nodes and attributes.
EXTENDED_LONG_NODES = 1

#: Fixed-width value types -> (struct format, python-side arity)
_FIXED = {
    DataType.BYTE: ("<B", 1), DataType.SHORT: ("<h", 1), DataType.USHORT: ("<H", 1),
    DataType.INT: ("<i", 1), DataType.UINT: ("<I", 1), DataType.FLOAT: ("<f", 1),
    DataType.DOUBLE: ("<d", 1), DataType.BOOL: ("<B", 1), DataType.ULONGLONG: ("<Q", 1),
    DataType.LONG: ("<q", 1), DataType.INT8: ("<b", 1), DataType.INT64: ("<q", 1),
    DataType.IVEC2: ("<2i", 2), DataType.IVEC3: ("<3i", 3), DataType.IVEC4: ("<4i", 4),
    DataType.VEC2: ("<2f", 2), DataType.VEC3: ("<3f", 3), DataType.VEC4: ("<4f", 4),
    DataType.MAT2: ("<4f", 4), DataType.MAT3: ("<9f", 9), DataType.MAT3X4: ("<12f", 12),
    DataType.MAT4X3: ("<12f", 12), DataType.MAT4: ("<16f", 16),
}

_STRING_TYPES = {
    DataType.STRING, DataType.PATH, DataType.FIXEDSTRING,
    DataType.LSSTRING, DataType.WSTRING, DataType.LSWSTRING,
}


class LSFError(Exception):
    pass


def split_sections(blob: bytes) -> tuple[dict[str, bytes], dict[str, bool]]:
    """Decompress an LSF file's five sections, honouring the on-disk ordering.

    Returns the raw section bytes keyed by name, plus which sections used LZ4
    frame framing.
    """
    if len(blob) < HEADER.size or blob[:4] != MAGIC:
        raise LSFError("not an LSF file")
    fields = HEADER.unpack_from(blob, 0)
    comp_flags = fields[13]
    declared = {name: (fields[3 + i * 2], fields[4 + i * 2])
                for i, name in enumerate(HEADER_ORDER)}

    pos = HEADER.size
    sections: dict[str, bytes] = {}
    chunked_map: dict[str, bool] = dict(DEFAULT_CHUNKED)
    for name in DISK_ORDER:
        unc, disk = declared[name]
        if disk == 0:
            # Stored verbatim (or absent entirely).
            sections[name] = blob[pos:pos + unc] if unc else b""
            pos += unc
            continue
        chunk = blob[pos:pos + disk]
        chunked = compression.is_lz4_frame(chunk)
        chunked_map[name] = chunked
        out = compression.decompress(chunk, comp_flags, unc, chunked)
        if len(out) != unc:
            raise LSFError(
                f"{name} section decompressed to {len(out)} bytes, header says {unc}"
            )
        sections[name] = out
        pos += disk
    return sections, chunked_map


# --------------------------------------------------------------------------
# value codecs
# --------------------------------------------------------------------------

def _read_value(buf: memoryview, offset: int, length: int, type_id: DataType) -> Any:
    end = offset + length
    if type_id == DataType.NONE:
        return None

    fixed = _FIXED.get(type_id)
    if fixed is not None:
        fmt, arity = fixed
        vals = struct.unpack_from(fmt, buf, offset)
        if type_id == DataType.BOOL:
            return bool(vals[0])
        return vals[0] if arity == 1 else list(vals)

    if type_id in _STRING_TYPES:
        raw = bytes(buf[offset:end])
        if raw.endswith(b"\x00"):
            raw = raw[:-1]
        return raw.decode("utf-8", "surrogateescape")

    if type_id == DataType.UUID:
        return decode_uuid(bytes(buf[offset:offset + 16]))

    if type_id == DataType.SCRATCHBUFFER:
        return bytes(buf[offset:end])

    if type_id == DataType.TRANSLATEDSTRING:
        s, _ = _read_translated_string(buf, offset)
        return s

    if type_id == DataType.TRANSLATEDFSSTRING:
        s, _ = _read_translated_fs_string(buf, offset)
        return s

    raise LSFError(f"cannot decode attribute type {int(type_id)}")


_UUID_WORDS = struct.Struct("<8H")


def decode_uuid(raw: bytes) -> str:
    """Format Larian's 16-byte GUID as the canonical string.

    Every 16-bit group is stored little-endian, so the bytes are read as eight
    uint16s and re-ordered.  Getting this wrong scrambles the last two groups
    and breaks cross-referencing against the game's data files.
    """
    h = _UUID_WORDS.unpack(raw)
    return (f"{h[1]:04x}{h[0]:04x}-{h[2]:04x}-{h[3]:04x}-"
            f"{h[4]:04x}-{h[5]:04x}{h[6]:04x}{h[7]:04x}")


def encode_uuid(value: str) -> bytes:
    text = str(value).replace("-", "").strip()
    if len(text) != 32:
        raise LSFError(f"invalid GUID {value!r}")
    w = [int(text[i:i + 4], 16) for i in range(0, 32, 4)]
    return _UUID_WORDS.pack(w[1], w[0], w[2], w[3], w[4], w[5], w[6], w[7])


def _decode_cstr(buf: memoryview, pos: int, length: int) -> str:
    raw = bytes(buf[pos:pos + length])
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    return raw.decode("utf-8", "surrogateescape")


def _read_translated_string(buf: memoryview, pos: int) -> tuple[TranslatedString, int]:
    """Read `uint16 version, int32 handleLength, char[handleLength] handle`.

    BG3 keeps only the loca handle here; the displayed text lives in the .loca
    tables, so there is no inline value field.
    """
    s = TranslatedString()
    (s.version,) = struct.unpack_from("<H", buf, pos)
    pos += 2
    (hlen,) = struct.unpack_from("<i", buf, pos)
    pos += 4
    s.handle = _decode_cstr(buf, pos, hlen)
    pos += hlen
    return s, pos


def _read_translated_fs_string(buf: memoryview, pos: int) -> tuple[TranslatedFSString, int]:
    base, pos = _read_translated_string(buf, pos)
    s = TranslatedFSString(version=base.version, value=base.value, handle=base.handle)
    (count,) = struct.unpack_from("<i", buf, pos)
    pos += 4
    for _ in range(count):
        arg = TranslatedFSStringArgument()
        (klen,) = struct.unpack_from("<i", buf, pos)
        pos += 4
        arg.key = _decode_cstr(buf, pos, klen)
        pos += klen
        arg.string, pos = _read_translated_fs_string(buf, pos)
        (vlen,) = struct.unpack_from("<i", buf, pos)
        pos += 4
        arg.value = _decode_cstr(buf, pos, vlen)
        pos += vlen
        s.arguments.append(arg)
    return s, pos


def _write_value(value: Any, type_id: DataType) -> bytes:
    if type_id == DataType.NONE:
        return b""

    fixed = _FIXED.get(type_id)
    if fixed is not None:
        fmt, arity = fixed
        if type_id == DataType.BOOL:
            return struct.pack(fmt, 1 if value else 0)
        if arity == 1:
            if type_id in (DataType.FLOAT, DataType.DOUBLE):
                return struct.pack(fmt, float(value))
            return struct.pack(fmt, int(value))
        return struct.pack(fmt, *value)

    if type_id in _STRING_TYPES:
        return value.encode("utf-8", "surrogateescape") + b"\x00"

    if type_id == DataType.UUID:
        return encode_uuid(value)

    if type_id == DataType.SCRATCHBUFFER:
        return bytes(value)

    if type_id == DataType.TRANSLATEDSTRING:
        return _write_translated_string(value)

    if type_id == DataType.TRANSLATEDFSSTRING:
        return _write_translated_fs_string(value)

    raise LSFError(f"cannot encode attribute type {int(type_id)}")


def _cstr(s: str) -> bytes:
    return s.encode("utf-8", "surrogateescape") + b"\x00"


def _write_translated_string(s: TranslatedString) -> bytes:
    handle = _cstr(s.handle)
    return struct.pack("<Hi", s.version, len(handle)) + handle


def _write_translated_fs_string(s: TranslatedFSString) -> bytes:
    out = _write_translated_string(s)
    out += struct.pack("<i", len(s.arguments))
    for arg in s.arguments:
        key = _cstr(arg.key)
        out += struct.pack("<i", len(key)) + key
        out += _write_translated_fs_string(arg.string or TranslatedFSString())
        val = _cstr(arg.value)
        out += struct.pack("<i", len(val)) + val
    return out


# --------------------------------------------------------------------------
# string hash table
# --------------------------------------------------------------------------

def _read_string_table(buf: bytes) -> list[list[str]]:
    if not buf:
        return []
    pos = 0
    (num_buckets,) = struct.unpack_from("<I", buf, pos)
    pos += 4
    buckets: list[list[str]] = []
    for _ in range(num_buckets):
        (count,) = struct.unpack_from("<H", buf, pos)
        pos += 2
        entries = []
        for _ in range(count):
            (length,) = struct.unpack_from("<H", buf, pos)
            pos += 2
            entries.append(buf[pos:pos + length].decode("utf-8", "surrogateescape"))
            pos += length
        buckets.append(entries)
    return buckets


def _write_string_table(buckets: list[list[str]]) -> bytes:
    out = bytearray(struct.pack("<I", len(buckets)))
    for entries in buckets:
        out += struct.pack("<H", len(entries))
        for s in entries:
            raw = s.encode("utf-8", "surrogateescape")
            out += struct.pack("<H", len(raw)) + raw
    return bytes(out)


class _StringPool:
    """Maps names to (bucket, offset) references, extending the original table."""

    def __init__(self, buckets: list[list[str]]):
        self.buckets = [list(b) for b in buckets] or [[] for _ in range(512)]
        self._index: dict[str, int] = {}
        for bi, entries in enumerate(self.buckets):
            for oi, s in enumerate(entries):
                self._index.setdefault(s, (bi << 16) | oi)

    def ref(self, name: str) -> int:
        existing = self._index.get(name)
        if existing is not None:
            return existing
        # New name: append to the least populated bucket to keep lookups even.
        bi = min(range(len(self.buckets)), key=lambda i: len(self.buckets[i]))
        oi = len(self.buckets[bi])
        if oi > 0xFFFF:
            raise LSFError("string bucket overflow")
        self.buckets[bi].append(name)
        ref = (bi << 16) | oi
        self._index[name] = ref
        return ref

    def name(self, ref: int) -> str:
        bi, oi = ref >> 16, ref & 0xFFFF
        try:
            return self.buckets[bi][oi]
        except IndexError as exc:
            raise LSFError(f"bad string reference {bi}/{oi}") from exc


# --------------------------------------------------------------------------
# document
# --------------------------------------------------------------------------

@dataclass
class LSFDocument:
    """A parsed LSF file, retaining enough header state to rewrite it faithfully."""

    resource: Resource = field(default_factory=Resource)
    version: int = 7
    engine_version: int = 0x020300000000012C
    compression_flags: int = compression.make_flags(compression.LZ4, compression.DEFAULT)
    unknown: tuple[int, int, int] = (0, 0, 0)
    extended: int = 0
    string_buckets: list[list[str]] = field(default_factory=list)
    keys: list[tuple[int, str]] = field(default_factory=list)
    #: Per-section LZ4 framing, keyed by section name.
    section_chunked: dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_CHUNKED))

    # ---------------------------------------------------------------- read
    @classmethod
    def from_bytes(cls, blob: bytes) -> "LSFDocument":
        if len(blob) < HEADER.size or blob[:4] != MAGIC:
            raise LSFError("not an LSF file")

        (_, version, engine_version,
         str_unc, str_disk, key_unc, key_disk, node_unc, node_disk,
         attr_unc, attr_disk, val_unc, val_disk,
         comp_flags, unk1, unk2, unk3, extended) = HEADER.unpack_from(blob, 0)

        if version not in SUPPORTED_VERSIONS:
            raise LSFError(f"unsupported LSF version {version}")

        doc = cls(version=version, engine_version=engine_version,
                  compression_flags=comp_flags, unknown=(unk1, unk2, unk3),
                  extended=extended)

        sections, doc.section_chunked = split_sections(blob)

        strings_buf = sections["strings"]
        keys_buf = sections["keys"]
        nodes_buf = sections["nodes"]
        attrs_buf = sections["attributes"]
        values_buf = sections["values"]

        doc.string_buckets = _read_string_table(strings_buf)
        pool = _StringPool(doc.string_buckets)

        for off in range(0, len(keys_buf) - KEY_ENTRY.size + 1, KEY_ENTRY.size):
            node_index, name_ref = KEY_ENTRY.unpack_from(keys_buf, off)
            doc.keys.append((node_index, pool.name(name_ref)))

        long_form = extended == EXTENDED_LONG_NODES
        node_struct = NODE_V3 if long_form else NODE_V2
        attr_struct = ATTR_V3 if long_form else ATTR_V2

        if len(nodes_buf) % node_struct.size:
            raise LSFError(
                f"nodes section ({len(nodes_buf)} bytes) is not a multiple of "
                f"{node_struct.size}; wrong extended flag?"
            )
        if len(attrs_buf) % attr_struct.size:
            raise LSFError(
                f"attributes section ({len(attrs_buf)} bytes) is not a multiple of "
                f"{attr_struct.size}; wrong extended flag?"
            )

        # --- nodes ---
        raw_nodes = []
        for off in range(0, len(nodes_buf), node_struct.size):
            if long_form:
                name_ref, parent, _sibling, first_attr = node_struct.unpack_from(nodes_buf, off)
            else:
                name_ref, first_attr, parent = node_struct.unpack_from(nodes_buf, off)
            raw_nodes.append((pool.name(name_ref), parent, first_attr))

        nodes = [Node(name) for name, _, _ in raw_nodes]

        # --- attributes ---
        view = memoryview(values_buf)
        per_node: dict[int, list[Attribute]] = {}

        if long_form:
            # Explicit offsets and a per-node linked list.
            entries = []
            for off in range(0, len(attrs_buf), attr_struct.size):
                name_ref, type_len, next_index, data_off = attr_struct.unpack_from(attrs_buf, off)
                entries.append((pool.name(name_ref), type_len & 0x3F,
                                type_len >> 6, next_index, data_off))
            for node_index, (_, _, first_attr) in enumerate(raw_nodes):
                idx = first_attr
                while idx != -1:
                    name, type_id, length, next_index, data_off = entries[idx]
                    per_node.setdefault(node_index, []).append(Attribute(
                        name, DataType(type_id),
                        _read_value(view, data_off, length, DataType(type_id))))
                    idx = next_index
        else:
            # Compact form: attributes in file order, values laid out end to end.
            data_off = 0
            for off in range(0, len(attrs_buf), attr_struct.size):
                name_ref, type_len, node_index = attr_struct.unpack_from(attrs_buf, off)
                type_id = DataType(type_len & 0x3F)
                length = type_len >> 6
                per_node.setdefault(node_index, []).append(Attribute(
                    name=pool.name(name_ref),
                    type=type_id,
                    value=_read_value(view, data_off, length, type_id),
                ))
                data_off += length
            if data_off != len(values_buf):
                raise LSFError(
                    f"values section not fully consumed: read {data_off} of {len(values_buf)}"
                )

        for node_index, attrs in per_node.items():
            target = nodes[node_index].attributes
            for attr in attrs:
                target[attr.name] = attr

        # --- tree ---
        for index, (name, parent, _) in enumerate(raw_nodes):
            if parent == -1:
                doc.resource.regions[name] = nodes[index]
            else:
                nodes[parent].append(nodes[index])

        doc.resource.metadata = ResourceMetadata(
            major=(engine_version >> 55) & 0x7F,
            minor=(engine_version >> 47) & 0xFF,
            revision=(engine_version >> 31) & 0xFFFF,
            build=engine_version & 0x7FFFFFFF,
        )
        return doc

    # --------------------------------------------------------------- write
    def to_bytes(self) -> bytes:
        pool = _StringPool(self.string_buckets)

        flat: list[tuple[Node, int]] = []       # (node, parent index)
        order: dict[int, int] = {}              # id(node) -> index

        def visit(node: Node, parent_index: int) -> None:
            index = len(flat)
            flat.append((node, parent_index))
            order[id(node)] = index
            for children in node.children.values():
                for child in children:
                    visit(child, index)

        for region in self.resource.regions.values():
            visit(region, -1)

        long_form = self.extended == EXTENDED_LONG_NODES
        nodes_buf = bytearray()
        attrs_buf = bytearray()
        values_buf = bytearray()

        if long_form:
            # Emit each node's attributes as a contiguous run and link them.
            attr_index = 0
            node_first: list[int] = []
            attr_records: list[bytes] = []
            for node, _ in flat:
                if not node.attributes:
                    node_first.append(-1)
                    continue
                node_first.append(attr_index)
                items = list(node.attributes.values())
                for i, attr in enumerate(items):
                    payload = _write_value(attr.value, attr.type)
                    next_index = attr_index + 1 if i + 1 < len(items) else -1
                    attr_records.append(ATTR_V3.pack(
                        pool.ref(attr.name),
                        (int(attr.type) & 0x3F) | (len(payload) << 6),
                        next_index, len(values_buf),
                    ))
                    values_buf += payload
                    attr_index += 1
            for record in attr_records:
                attrs_buf += record
            for index, (node, parent_index) in enumerate(flat):
                sibling = -1
                if parent_index != -1:
                    siblings = [order[id(c)]
                                for group in flat[parent_index][0].children.values()
                                for c in group]
                    pos = siblings.index(index)
                    sibling = siblings[pos + 1] if pos + 1 < len(siblings) else -1
                nodes_buf += NODE_V3.pack(pool.ref(node.name), parent_index,
                                          sibling, node_first[index])
        else:
            first_attr = [-1] * len(flat)
            for index, (node, _) in enumerate(flat):
                if not node.attributes:
                    continue
                first_attr[index] = len(attrs_buf) // ATTR_V2.size
                for attr in node.attributes.values():
                    payload = _write_value(attr.value, attr.type)
                    attrs_buf += ATTR_V2.pack(
                        pool.ref(attr.name),
                        (int(attr.type) & 0x3F) | (len(payload) << 6),
                        index,
                    )
                    values_buf += payload
            for index, (node, parent_index) in enumerate(flat):
                nodes_buf += NODE_V2.pack(pool.ref(node.name),
                                          first_attr[index], parent_index)

        keys_buf = bytearray()
        for node_index, key_name in self.keys:
            keys_buf += KEY_ENTRY.pack(node_index, pool.ref(key_name))

        # The string table must be serialised last: encoding names above may
        # have appended new entries to the pool.
        strings_buf = _write_string_table(pool.buckets)

        raw_sections = {
            "strings": strings_buf, "keys": bytes(keys_buf),
            "nodes": bytes(nodes_buf), "attributes": bytes(attrs_buf),
            "values": bytes(values_buf),
        }

        packed: dict[str, bytes] = {}
        sizes: dict[str, tuple[int, int]] = {}
        for name in DISK_ORDER:
            raw = raw_sections[name]
            if not raw:
                sizes[name] = (0, 0)
                packed[name] = b""
                continue
            blob = compression.compress(
                raw, self.compression_flags,
                chunked=self.section_chunked.get(name, DEFAULT_CHUNKED[name]))
            sizes[name] = (len(raw), len(blob))
            packed[name] = blob

        header = HEADER.pack(
            MAGIC, self.version, self.engine_version,
            *(n for name in HEADER_ORDER for n in sizes[name]),
            self.compression_flags, *self.unknown, self.extended,
        )
        return header + b"".join(packed[name] for name in DISK_ORDER)


def loads(blob: bytes) -> LSFDocument:
    return LSFDocument.from_bytes(blob)


def dumps(doc: LSFDocument) -> bytes:
    return doc.to_bytes()
