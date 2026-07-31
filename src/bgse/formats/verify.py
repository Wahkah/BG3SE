"""Self-checks for the LSF codecs.

`check_codecs` re-encodes every attribute value and compares the byte length
against what the file declared.  Any mismatch means a value codec is wrong, so
this is the first thing to run when a new game patch changes the format.
"""

from __future__ import annotations

import struct

from .lsf import ATTR_V2, ATTR_V3, LSFDocument, split_sections, _read_value, _write_value
from .resource import DataType, Node, Resource


def check_codecs(blob: bytes) -> list[str]:
    """Return a list of human-readable codec problems (empty when all good)."""
    doc = LSFDocument.from_bytes(blob)
    sections, _ = split_sections(blob)
    attrs_buf, values_buf = sections["attributes"], sections["values"]
    view = memoryview(values_buf)

    problems: list[str] = []
    struct_ = ATTR_V3 if doc.extended else ATTR_V2
    offset = 0
    for index, off in enumerate(range(0, len(attrs_buf), struct_.size)):
        if doc.extended:
            _, type_len, _, data_off = struct_.unpack_from(attrs_buf, off)
        else:
            _, type_len, _ = struct_.unpack_from(attrs_buf, off)
            data_off = offset
        type_id = DataType(type_len & 0x3F)
        length = type_len >> 6
        offset += length
        try:
            value = _read_value(view, data_off, length, type_id)
            encoded = _write_value(value, type_id)
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            problems.append(f"attribute #{index} ({type_id.name}): {exc}")
            continue
        if len(encoded) != length:
            problems.append(
                f"attribute #{index} ({type_id.name}): declared {length} bytes, "
                f"re-encoded to {len(encoded)}"
            )
    return problems


def diff_resources(a: Resource, b: Resource, limit: int = 20) -> list[str]:
    """Deep-compare two resources, returning up to `limit` differences."""
    problems: list[str] = []

    def note(msg: str) -> bool:
        problems.append(msg)
        return len(problems) >= limit

    def cmp_node(x: Node, y: Node, path: str) -> bool:
        if x.name != y.name and note(f"{path}: node name {x.name!r} != {y.name!r}"):
            return True
        if list(x.attributes) != list(y.attributes) and note(
                f"{path}: attribute names differ"):
            return True
        for key, xa in x.attributes.items():
            ya = y.attributes[key]
            if xa.type != ya.type and note(
                    f"{path}/{key}: type {xa.type.name} != {ya.type.name}"):
                return True
            if xa.value != ya.value and note(
                    f"{path}/{key}: value {xa.value!r} != {ya.value!r}"):
                return True
        if list(x.children) != list(y.children) and note(
                f"{path}: child names differ"):
            return True
        for key, xs in x.children.items():
            ys = y.children[key]
            if len(xs) != len(ys) and note(
                    f"{path}/{key}: {len(xs)} children != {len(ys)}"):
                return True
            for i, (xc, yc) in enumerate(zip(xs, ys)):
                if cmp_node(xc, yc, f"{path}/{key}[{i}]"):
                    return True
        return False

    if list(a.regions) != list(b.regions):
        return [f"regions differ: {list(a.regions)} != {list(b.regions)}"]
    for name, region in a.regions.items():
        if cmp_node(region, b.regions[name], name):
            break
    return problems


def roundtrip(blob: bytes) -> list[str]:
    """Parse, rewrite, re-parse and confirm the tree survived unchanged."""
    first = LSFDocument.from_bytes(blob)
    second = LSFDocument.from_bytes(first.to_bytes())
    return diff_resources(first.resource, second.resource)
