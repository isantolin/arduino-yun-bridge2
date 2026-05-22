"""Run-Length Encoding (RLE) logic for MCU Bridge RPC communication.

This module implements a simple but efficient RLE compression/decompression
algorithm designed for low-memory microcontrollers (SIL-2).

Format:
- Escape Byte (0xFF)
- Count-2 (1 byte): How many times the value is repeated (beyond the first 2).
- Value (1 byte): The byte value being repeated.
- Special: If Count-2 == 255, it's a single literal escape byte.
"""

from __future__ import annotations

import itertools

from . import protocol

import re

RLE_MAX_CHUNK_COUNT = 254

# [SIL-2] Pre-compiled regex to find compressible sequences.
# Pattern matches any byte (.) followed by the same byte at least N-1 times.
# For RLE_MIN_RUN_LENGTH = 4, the pattern is b'(.)\\1{3,}'
_RLE_PATTERN = re.compile(rb"(.)\1{" + str(protocol.RLE_MIN_RUN_LENGTH - 1).encode() + rb",}")


def rle_decode(obj: bytes | bytearray | memoryview) -> bytes:
    """Decompress RLE data natively using iterators (SIL-2)."""
    if not obj:
        return b""

    res = bytearray()
    it = iter(obj)
    for val in it:
        if val == protocol.RLE_ESCAPE_BYTE:
            try:
                count_m2 = next(it)
                c_val = next(it)
            except StopIteration:
                raise ValueError("RLE decode failed: incomplete escape sequence")

            if count_m2 == protocol.RLE_SINGLE_ESCAPE_MARKER:
                res.append(protocol.RLE_ESCAPE_BYTE)
            else:
                res.extend(bytes([c_val]) * (count_m2 + protocol.RLE_OFFSET))
        else:
            res.append(val)

    return bytes(res)


def rle_encode(obj: bytes | bytearray | memoryview) -> bytes:
    """Compress data using optimized groupby and bulk extensions."""
    if not obj:
        return b""

    res = bytearray()

    # Optimized encoding using the same pattern as should_compress
    # to avoid manual byte-by-byte iteration when possible.
    for byte_val, group in itertools.groupby(obj):
        run_len = sum(1 for _ in group)

        if byte_val == protocol.RLE_ESCAPE_BYTE:
            # Escape literal 0xFF as [0xFF, 0xFF, 0xFF]
            marker = bytes([protocol.RLE_ESCAPE_BYTE, protocol.RLE_SINGLE_ESCAPE_MARKER, byte_val])
            res.extend(marker * run_len)
        elif run_len >= protocol.RLE_MIN_RUN_LENGTH:
            # Handle chunks. Max count_m2 is RLE_MAX_CHUNK_COUNT to avoid SINGLE_ESCAPE_MARKER (255).
            # Max chunk size is RLE_MAX_CHUNK_COUNT + OFFSET.
            while run_len >= protocol.RLE_MIN_RUN_LENGTH:
                chunk = min(run_len, RLE_MAX_CHUNK_COUNT + protocol.RLE_OFFSET)
                res.append(protocol.RLE_ESCAPE_BYTE)
                res.append(chunk - protocol.RLE_OFFSET)
                res.append(byte_val)
                run_len -= chunk
            if run_len > 0:
                res.extend(bytes([byte_val] * run_len))
        else:
            res.extend(bytes([byte_val] * run_len))

    return bytes(res)


def should_compress(payload: bytes | bytearray | memoryview) -> bool:
    """Check if a payload should be RLE compressed using native C regex."""
    if len(payload) < protocol.RLE_MIN_COMPRESS_INPUT_SIZE:
        return False
    # Fast regex search for repeated runs of any byte
    return bool(_RLE_PATTERN.search(payload))


__all__ = ["rle_encode", "rle_decode", "should_compress"]
