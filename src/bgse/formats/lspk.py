"""Reader/writer for Larian LSPK packages (.pak archives and .lsv savegames).

Layout (version 18, used by BG3):

    offset  0   char[4]  'LSPK'
            4   uint32   version
            8   uint64   file list offset
           16   uint32   file list size
           20   uint8    flags
           21   uint8    priority
           22   byte[16] md5
           38   uint16   number of parts
           40            file data begins

The file list itself is an LZ4 block holding `numFiles` fixed 272-byte entries.
Payloads are decompressed lazily and re-emitted verbatim unless replaced, so
rewriting a save only re-compresses what actually changed.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

import lz4.block

from . import compression

MAGIC = b"LSPK"
HEADER_V18 = struct.Struct("<4sIQIBB16sH")
assert HEADER_V18.size == 40

ENTRY_V18 = struct.Struct("<256sIHBBII")
assert ENTRY_V18.size == 272

#: Payloads observed in real saves are aligned to 8-byte boundaries.
DATA_ALIGNMENT = 8

SUPPORTED_VERSIONS = (18,)


class PackageError(Exception):
    pass


@dataclass
class PackagedFile:
    """One entry in a package.  Holds compressed bytes until data is needed."""

    name: str
    flags: int = compression.make_flags(compression.ZSTD, compression.DEFAULT)
    part: int = 0
    _stored: bytes = b""          # bytes exactly as they appear on disk
    _uncompressed_size: int = 0
    _plain: bytes | None = None   # populated on first access / replacement
    _dirty: bool = False
    #: Set for lazily-read archives: read the payload from disk on demand
    #: instead of holding it, so a 12 GB .pak costs almost no memory.
    _source: Path | None = None
    _offset: int = 0
    _disk_size: int = 0

    # --- reading ----------------------------------------------------------
    def _load_stored(self) -> bytes:
        if not self._stored and self._source is not None and self._disk_size:
            with open(self._source, "rb") as fh:
                fh.seek(self._offset)
                self._stored = fh.read(self._disk_size)
        return self._stored

    @property
    def data(self) -> bytes:
        if self._plain is None:
            self._plain = compression.decompress(
                self._load_stored(), self.flags, self._uncompressed_size
            )
        return self._plain

    @property
    def size(self) -> int:
        return self._uncompressed_size

    # --- writing ----------------------------------------------------------
    @data.setter
    def data(self, value: bytes) -> None:
        self._plain = bytes(value)
        self._uncompressed_size = len(self._plain)
        self._dirty = True

    def stored(self) -> bytes:
        """Bytes to write out, re-compressing only if the payload changed."""
        if self._dirty:
            self._stored = compression.compress(self._plain or b"", self.flags)
            self._dirty = False
        return self._load_stored()

    def __repr__(self) -> str:
        return f"<PackagedFile {self.name!r} {self._uncompressed_size} bytes>"


class Package:
    """An LSPK archive, kept fully in memory."""

    def __init__(self) -> None:
        self.version = 18
        self.flags = 0
        self.priority = 0
        self.md5 = b"\x00" * 16
        self.files: list[PackagedFile] = []

    # ------------------------------------------------------------------ read
    @classmethod
    def read(cls, path: str | os.PathLike) -> "Package":
        return cls.from_bytes(Path(path).read_bytes())

    @classmethod
    def open(cls, path: str | os.PathLike) -> "Package":
        """Read only the header and file list; payloads load on demand.

        Game archives run to several gigabytes, so they must never be read
        whole.  The returned package is read-only in practice.
        """
        path = Path(path)
        with open(path, "rb") as fh:
            head = fh.read(HEADER_V18.size)
            if len(head) < HEADER_V18.size:
                raise PackageError("file is too small to be an LSPK package")
            magic, version, list_offset, list_size, flags, priority, md5, parts = \
                HEADER_V18.unpack_from(head, 0)
            if magic != MAGIC:
                raise PackageError(f"not an LSPK package (magic {magic!r})")
            if version not in SUPPORTED_VERSIONS:
                raise PackageError(f"unsupported package version {version}")
            if parts != 1:
                raise PackageError(f"multi-part packages are not supported ({parts})")

            fh.seek(list_offset)
            num_files, compressed_size = struct.unpack("<II", fh.read(8))
            raw = lz4.block.decompress(
                fh.read(compressed_size),
                uncompressed_size=ENTRY_V18.size * num_files,
            )

        pkg = cls()
        pkg.version = version
        pkg.flags = flags
        pkg.priority = priority
        pkg.md5 = md5
        for i in range(num_files):
            name_raw, off_lo, off_hi, part, eflags, on_disk, uncompressed = \
                ENTRY_V18.unpack_from(raw, i * ENTRY_V18.size)
            pkg.files.append(PackagedFile(
                name=name_raw.split(b"\x00", 1)[0].decode("utf-8"),
                flags=eflags,
                part=part,
                _uncompressed_size=uncompressed,
                _source=path,
                _offset=off_lo | (off_hi << 32),
                _disk_size=on_disk,
            ))
        return pkg

    @classmethod
    def from_bytes(cls, blob: bytes) -> "Package":
        if len(blob) < HEADER_V18.size:
            raise PackageError("file is too small to be an LSPK package")

        magic, version, list_offset, list_size, flags, priority, md5, parts = \
            HEADER_V18.unpack_from(blob, 0)
        if magic != MAGIC:
            raise PackageError(f"not an LSPK package (magic {magic!r})")
        if version not in SUPPORTED_VERSIONS:
            raise PackageError(
                f"unsupported package version {version}; expected one of {SUPPORTED_VERSIONS}"
            )
        if parts != 1:
            raise PackageError(f"multi-part packages are not supported ({parts} parts)")
        if list_offset + 8 > len(blob):
            raise PackageError("file list offset lies outside the file")

        pkg = cls()
        pkg.version = version
        pkg.flags = flags
        pkg.priority = priority
        pkg.md5 = md5

        num_files, compressed_size = struct.unpack_from("<II", blob, list_offset)
        raw = lz4.block.decompress(
            blob[list_offset + 8: list_offset + 8 + compressed_size],
            uncompressed_size=ENTRY_V18.size * num_files,
        )

        for i in range(num_files):
            name_raw, off_lo, off_hi, part, eflags, on_disk, uncompressed = \
                ENTRY_V18.unpack_from(raw, i * ENTRY_V18.size)
            name = name_raw.split(b"\x00", 1)[0].decode("utf-8")
            offset = off_lo | (off_hi << 32)
            if offset + on_disk > len(blob):
                raise PackageError(f"entry {name!r} extends past end of file")
            pkg.files.append(PackagedFile(
                name=name,
                flags=eflags,
                part=part,
                _stored=blob[offset: offset + on_disk],
                _uncompressed_size=uncompressed,
            ))
        return pkg

    # ----------------------------------------------------------------- write
    def to_bytes(self) -> bytes:
        out = io.BytesIO()
        out.write(b"\x00" * HEADER_V18.size)

        entries = bytearray()
        for f in self.files:
            payload = f.stored()
            pad = (-out.tell()) % DATA_ALIGNMENT
            if pad:
                out.write(b"\x00" * pad)
            offset = out.tell()
            out.write(payload)

            name = f.name.encode("utf-8")
            if len(name) > 255:
                raise PackageError(f"entry name too long: {f.name!r}")
            entries += ENTRY_V18.pack(
                name.ljust(256, b"\x00"),
                offset & 0xFFFFFFFF,
                (offset >> 32) & 0xFFFF,
                f.part,
                f.flags,
                len(payload),
                f.size,
            )

        pad = (-out.tell()) % DATA_ALIGNMENT
        if pad:
            out.write(b"\x00" * pad)
        list_offset = out.tell()
        compressed_list = lz4.block.compress(bytes(entries), store_size=False)
        out.write(struct.pack("<II", len(self.files), len(compressed_list)))
        out.write(compressed_list)
        list_size = out.tell() - list_offset

        out.seek(0)
        out.write(HEADER_V18.pack(
            MAGIC, self.version, list_offset, list_size,
            self.flags, self.priority, self.md5, 1,
        ))
        return out.getvalue()

    def write(self, path: str | os.PathLike) -> None:
        Path(path).write_bytes(self.to_bytes())

    # ------------------------------------------------------------- accessors
    def __contains__(self, name: str) -> bool:
        return any(f.name == name for f in self.files)

    def __getitem__(self, name: str) -> PackagedFile:
        for f in self.files:
            if f.name == name:
                return f
        raise KeyError(name)

    def get(self, name: str) -> PackagedFile | None:
        try:
            return self[name]
        except KeyError:
            return None

    def names(self) -> list[str]:
        return [f.name for f in self.files]

    def __repr__(self) -> str:
        return f"<Package v{self.version} files={len(self.files)}>"
