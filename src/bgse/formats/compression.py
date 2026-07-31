"""Compression codecs used by Larian's LSPK/LSF containers.

A single byte carries both the algorithm (low nibble) and the effort level
(high nibble).  BG3 savegames use zstd for the outer package and LZ4 for the
sections inside each LSF file.
"""

from __future__ import annotations

import zlib

import lz4.block
import lz4.frame
import zstandard

#: LSF v6+ stores its node/attribute/value substreams as LZ4 *frames* while the
#: string table stays a raw LZ4 block, so framing is detected per stream.
LZ4_FRAME_MAGIC = b"\x04\x22\x4d\x18"

# --- low nibble: algorithm -------------------------------------------------
NONE = 0
ZLIB = 1
LZ4 = 2
ZSTD = 3

# --- high nibble: effort ---------------------------------------------------
FAST = 1
DEFAULT = 2
MAX = 3

_ZSTD_LEVEL = {FAST: 1, DEFAULT: 6, MAX: 19}
_ZLIB_LEVEL = {FAST: 1, DEFAULT: 6, MAX: 9}

METHOD_NAMES = {NONE: "none", ZLIB: "zlib", LZ4: "lz4", ZSTD: "zstd"}


def method(flags: int) -> int:
    return flags & 0x0F


def level(flags: int) -> int:
    return (flags >> 4) & 0x0F


def make_flags(method_id: int, level_id: int = DEFAULT) -> int:
    return (method_id & 0x0F) | ((level_id & 0x0F) << 4)


def is_lz4_frame(data: bytes) -> bool:
    return data[:4] == LZ4_FRAME_MAGIC


def decompress(data: bytes, flags: int, uncompressed_size: int,
               chunked: bool | None = None) -> bytes:
    """Decompress `data` according to `flags`.

    `uncompressed_size` is mandatory for raw LZ4 blocks, which carry no length
    header.  `chunked` selects LZ4 frame framing; when omitted it is detected
    from the frame magic.
    """
    m = method(flags)
    if m == NONE:
        return data
    if m == ZLIB:
        out = zlib.decompress(data)
    elif m == LZ4:
        if chunked is None:
            chunked = is_lz4_frame(data)
        if chunked:
            out = lz4.frame.decompress(data)
        else:
            # Raw block framing: the output size must be supplied.
            return lz4.block.decompress(data, uncompressed_size=uncompressed_size)
    elif m == ZSTD:
        out = zstandard.ZstdDecompressor().decompress(
            data, max_output_size=max(uncompressed_size, 1)
        )
    else:
        raise ValueError(f"unsupported compression method {m}")

    if uncompressed_size and len(out) != uncompressed_size:
        raise ValueError(
            f"decompressed size mismatch: got {len(out)}, expected {uncompressed_size}"
        )
    return out


def compress(data: bytes, flags: int, chunked: bool = False) -> bytes:
    """Compress `data` according to `flags`."""
    m = method(flags)
    if m == NONE:
        return data
    lvl = level(flags) or DEFAULT
    if m == ZLIB:
        return zlib.compress(data, _ZLIB_LEVEL.get(lvl, 6))
    if m == LZ4:
        if chunked:
            # Matches the frame Larian emits: 64 KB linked blocks, no checksums
            # and no content-size field (FLG 0x40, BD 0x40).
            return lz4.frame.compress(
                data,
                block_size=lz4.frame.BLOCKSIZE_MAX64KB,
                block_linked=True,
                content_checksum=False,
                block_checksum=False,
                store_size=False,
            )
        # store_size=False keeps the raw block framing the engine expects.
        return lz4.block.compress(data, mode="high_compression" if lvl == MAX else "default",
                                  store_size=False)
    if m == ZSTD:
        return zstandard.ZstdCompressor(level=_ZSTD_LEVEL.get(lvl, 6)).compress(data)
    raise ValueError(f"unsupported compression method {m}")
