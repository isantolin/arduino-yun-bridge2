"""Run-Length Encoding (RLE) logic for MCU Bridge RPC communication.

This module implements a simple but efficient RLE compression/decompression
algorithm designed for low-memory microcontrollers (SIL-2).

Format:
- Escape Byte (0xFD)
- Count-2 (1 byte): How many times the value is repeated (beyond the first 2).
- Value (1 byte): The byte value being repeated.
- Special: If Count-2 == 255, it's a single literal escape byte.
"""

from __future__ import annotations

import itertools

from . import protocol


def rle_decode(obj: bytes | bytearray | memoryview) -> bytes:
    """Decompress RLE data natively."""
    if not obj:
        return b""

    res = bytearray()
    view = memoryview(obj)
    i = 0
    length = len(view)

    while i < length:
        val = view[i]
        if val == protocol.RLE_ESCAPE_BYTE:
            if i + 2 >= length:
                raise ValueError("RLE decode failed: incomplete escape sequence")
            count_m2 = view[i + 1]
            c_val = view[i + 2]

            if count_m2 == protocol.RLE_SINGLE_ESCAPE_MARKER:
                res.append(protocol.RLE_ESCAPE_BYTE)
            else:
                count = count_m2 + protocol.RLE_OFFSET
                res.extend(bytes([c_val]) * count)
            i += 3
        else:
            res.append(val)
            i += 1

    return bytes(res)


def rle_encode(obj: bytes | bytearray | memoryview) -> bytes:
    """Compress data using efficient hybrid construction (High Performance)."""
    if not obj:
        return b""

    res = bytearray()

    for byte_val, group in itertools.groupby(obj):
        g_list = list(group)
        run_len = len(g_list)

        if byte_val == protocol.RLE_ESCAPE_BYTE:
            while run_len > 0:
                res.append(protocol.RLE_ESCAPE_BYTE)
                res.append(protocol.RLE_SINGLE_ESCAPE_MARKER)
                res.append(byte_val)
                run_len -= 1
        elif run_len >= protocol.RLE_MIN_RUN_LENGTH:
            while run_len >= protocol.RLE_MIN_RUN_LENGTH:
                chunk_len = min(run_len, 256)
                res.append(protocol.RLE_ESCAPE_BYTE)
                res.append(chunk_len - protocol.RLE_OFFSET)
                res.append(byte_val)
                run_len -= chunk_len
            if run_len > 0:
                res.extend(bytes([byte_val] * run_len))
        else:
            res.extend(g_list)

    return bytes(res)


def should_compress(payload: bytes | bytearray | memoryview) -> bool:
    """Check if a payload should be RLE compressed."""
    if len(payload) < protocol.RLE_MIN_COMPRESS_INPUT_SIZE:
        return False
    for _, group in itertools.groupby(payload):
        if sum(1 for _ in group) >= protocol.RLE_MIN_RUN_LENGTH:
            return True
    return False


__all__ = ["rle_encode", "rle_decode", "should_compress"]
