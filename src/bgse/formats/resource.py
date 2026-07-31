"""In-memory model for Larian resource files (the tree stored inside an LSF).

A resource is a set of named regions; each region is a `Node`, nodes hold
ordered attributes and named lists of child nodes.  This mirrors the LSX/LSF
data model closely enough to round-trip either format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterator


class DataType(IntEnum):
    NONE = 0
    BYTE = 1
    SHORT = 2
    USHORT = 3
    INT = 4
    UINT = 5
    FLOAT = 6
    DOUBLE = 7
    IVEC2 = 8
    IVEC3 = 9
    IVEC4 = 10
    VEC2 = 11
    VEC3 = 12
    VEC4 = 13
    MAT2 = 14
    MAT3 = 15
    MAT3X4 = 16
    MAT4X3 = 17
    MAT4 = 18
    BOOL = 19
    STRING = 20
    PATH = 21
    FIXEDSTRING = 22
    LSSTRING = 23
    ULONGLONG = 24
    SCRATCHBUFFER = 25
    LONG = 26
    INT8 = 27
    TRANSLATEDSTRING = 28
    WSTRING = 29
    LSWSTRING = 30
    UUID = 31
    INT64 = 32
    TRANSLATEDFSSTRING = 33


#: Names as they appear in LSX, so exports interoperate with other Larian tools.
TYPE_NAMES = {
    DataType.NONE: "None", DataType.BYTE: "uint8", DataType.SHORT: "int16",
    DataType.USHORT: "uint16", DataType.INT: "int32", DataType.UINT: "uint32",
    DataType.FLOAT: "float", DataType.DOUBLE: "double", DataType.IVEC2: "ivec2",
    DataType.IVEC3: "ivec3", DataType.IVEC4: "ivec4", DataType.VEC2: "fvec2",
    DataType.VEC3: "fvec3", DataType.VEC4: "fvec4", DataType.MAT2: "mat2x2",
    DataType.MAT3: "mat3x3", DataType.MAT3X4: "mat3x4", DataType.MAT4X3: "mat4x3",
    DataType.MAT4: "mat4x4", DataType.BOOL: "bool", DataType.STRING: "string",
    DataType.PATH: "path", DataType.FIXEDSTRING: "FixedString",
    DataType.LSSTRING: "LSString", DataType.ULONGLONG: "uint64",
    DataType.SCRATCHBUFFER: "ScratchBuffer", DataType.LONG: "old_int64",
    DataType.INT8: "int8", DataType.TRANSLATEDSTRING: "TranslatedString",
    DataType.WSTRING: "WString", DataType.LSWSTRING: "LSWString",
    DataType.UUID: "guid", DataType.INT64: "int64",
    DataType.TRANSLATEDFSSTRING: "TranslatedFSString",
}

NAME_TO_TYPE = {v: k for k, v in TYPE_NAMES.items()}

#: Types the UI can safely present as a plain editable scalar.
NUMERIC_TYPES = frozenset({
    DataType.BYTE, DataType.SHORT, DataType.USHORT, DataType.INT, DataType.UINT,
    DataType.FLOAT, DataType.DOUBLE, DataType.ULONGLONG, DataType.LONG,
    DataType.INT8, DataType.INT64,
})

STRING_TYPES = frozenset({
    DataType.STRING, DataType.PATH, DataType.FIXEDSTRING, DataType.LSSTRING,
    DataType.WSTRING, DataType.LSWSTRING,
})


@dataclass
class TranslatedString:
    """A localised string: a handle into the loca table, plus optional override."""
    version: int = 0
    value: str | None = None
    handle: str = ""

    def __str__(self) -> str:
        return self.value if self.value is not None else self.handle


@dataclass
class TranslatedFSString(TranslatedString):
    arguments: list["TranslatedFSStringArgument"] = field(default_factory=list)


@dataclass
class TranslatedFSStringArgument:
    key: str = ""
    string: TranslatedFSString | None = None
    value: str = ""


@dataclass
class Attribute:
    name: str
    type: DataType
    value: Any

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, str(int(self.type)))


class Node:
    """A node in the resource tree."""

    __slots__ = ("name", "attributes", "children", "parent")

    def __init__(self, name: str = "", parent: "Node | None" = None):
        self.name = name
        self.attributes: dict[str, Attribute] = {}
        self.children: dict[str, list[Node]] = {}
        self.parent = parent

    # --- attribute access -------------------------------------------------
    def get(self, name: str, default: Any = None) -> Any:
        attr = self.attributes.get(name)
        return default if attr is None else attr.value

    def set(self, name: str, value: Any, type: DataType | None = None) -> Attribute:
        """Set an attribute, keeping the existing type unless one is given."""
        existing = self.attributes.get(name)
        if type is None:
            if existing is None:
                raise KeyError(f"attribute {name!r} does not exist; a type is required")
            type = existing.type
        attr = Attribute(name, DataType(type), value)
        self.attributes[name] = attr
        return attr

    # --- child access -----------------------------------------------------
    def child(self, name: str) -> "Node | None":
        nodes = self.children.get(name)
        return nodes[0] if nodes else None

    def child_list(self, name: str) -> list["Node"]:
        return self.children.get(name, [])

    def append(self, node: "Node") -> "Node":
        node.parent = self
        self.children.setdefault(node.name, []).append(node)
        return node

    def remove(self, node: "Node") -> bool:
        siblings = self.children.get(node.name)
        if siblings and node in siblings:
            siblings.remove(node)
            if not siblings:
                del self.children[node.name]
            return True
        return False

    # --- traversal --------------------------------------------------------
    def walk(self) -> Iterator["Node"]:
        """Depth-first iteration over this node and all descendants."""
        yield self
        for nodes in self.children.values():
            for child in nodes:
                yield from child.walk()

    def find(self, name: str) -> Iterator["Node"]:
        """All descendants (and self) whose node name matches."""
        for node in self.walk():
            if node.name == name:
                yield node

    def path(self) -> str:
        parts, node = [], self
        while node is not None and node.name:
            parts.append(node.name)
            node = node.parent
        return "/".join(reversed(parts))

    def __repr__(self) -> str:
        return (f"<Node {self.name!r} attrs={len(self.attributes)} "
                f"children={sum(len(v) for v in self.children.values())}>")


@dataclass
class ResourceMetadata:
    timestamp: int = 0
    major: int = 4
    minor: int = 0
    revision: int = 0
    build: int = 0


class Resource:
    """A parsed Larian resource: named regions of nodes."""

    def __init__(self) -> None:
        self.metadata = ResourceMetadata()
        self.regions: dict[str, Node] = {}

    def walk(self) -> Iterator[Node]:
        for region in self.regions.values():
            yield from region.walk()

    def find(self, name: str) -> Iterator[Node]:
        for node in self.walk():
            if node.name == name:
                yield node

    def __repr__(self) -> str:
        return f"<Resource regions={list(self.regions)}>"
