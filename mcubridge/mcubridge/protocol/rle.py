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
from typing import Any

from construct import (
    Adapter,
    Check,
    Const,
    Construct,
    ExprAdapter,
    FocusedSeq,
    GreedyBytes,
    GreedyRange,
    Int8ub,
    Select,
    Struct,
    Terminated,
)
from construct.core import ConstructError

from . import protocol


class RleAdapter(Adapter):
    """[SIL-2] Construct Adapter for Run-Length Encoding transformation."""

    def _decode(self, obj: bytes, context: Any, path: Any) -> bytes:
        """Decompress RLE data using the declarative decoder."""
        if not obj:
            return b""
        try:
            # [SIL-2] Optimized: result is a list of byte chunks
            chunks = RLE_DECODER.parse(obj)
            return b"".join(chunks)
        except (ConstructError, ValueError, TypeError) as e:
            raise ValueError(f"RLE decode failed: {e}") from e

    def _encode(self, obj: bytes, context: Any, path: Any) -> bytes:
        """Compress data using efficient hybrid construction (High Performance)."""
        if not obj:
            return b""

        # [SIL-2] High Performance: Use bytearray for concatenation and
        # direct structural builds for escape sequences.
        res = bytearray()

        for byte_val, group in itertools.groupby(obj):
            g_list = list(group)
            run_len = len(g_list)

            if byte_val == protocol.RLE_ESCAPE_BYTE:
                # Escape byte must ALWAYS be escaped
                while run_len > 0:
                    res.extend(
                        RLE_ESCAPE_STRUCT.build(
                            {
                                "count_m2": protocol.RLE_SINGLE_ESCAPE_MARKER,
                                "value": byte_val,
                            }
                        )
                    )
                    run_len -= 1
            elif run_len >= protocol.RLE_MIN_RUN_LENGTH:
                # Long run of non-escape bytes
                while run_len >= protocol.RLE_MIN_RUN_LENGTH:
                    chunk_len = min(run_len, 256)
                    res.extend(
                        RLE_ESCAPE_STRUCT.build(
                            {
                                "count_m2": chunk_len - protocol.RLE_OFFSET,
                                "value": byte_val,
                            }
                        )
                    )
                    run_len -= chunk_len
                # Handle remaining few bytes as literals
                if run_len > 0:
                    res.extend(bytes([byte_val] * run_len))
            else:
                # Direct literal addition for short runs
                res.extend(g_list)

        return bytes(res)


def _rle_decode_chunk(obj: Any, _ctx: Any) -> bytes:
    """Decode an RLE escape sequence chunk."""
    if obj.count_m2 == protocol.RLE_SINGLE_ESCAPE_MARKER:
        return bytes([protocol.RLE_ESCAPE_BYTE])
    count = int(obj.count_m2) + protocol.RLE_OFFSET
    return bytes([obj.value]) * count


# [SIL-2] Highly Optimized RLE Structures
RLE_ESCAPE_STRUCT: Construct = Struct(
    "escape" / Const(protocol.RLE_ESCAPE_BYTE, Int8ub),
    "count_m2" / Int8ub,
    "value" / Int8ub,
)


def _is_not_esc_byte(ctx: Any) -> bool:
    """SIL-2: Verify the current byte is not the escape byte."""
    return ctx.val != protocol.RLE_ESCAPE_BYTE


def _decode_literal(obj: int, _ctx: Any) -> bytes:
    """SIL-2: Convert literal integer byte to bytes object."""
    return bytes([obj])


def _encode_literal(obj: bytes, _ctx: Any) -> int:
    """SIL-2: Convert literal bytes object to integer byte."""
    return int(obj[0])


# [SIL-2] Optimized Decoder Schema
# Pre-compilation and Select-ordering minimize overhead.
# Terminated ensures full stream consumption and detects malformed trailing data.
RLE_DECODER: Construct = FocusedSeq(
    "chunks",
    "chunks"
    / GreedyRange(
        Select(
            # Escape sequence: Try this first. It starts with the escape byte.
            ExprAdapter(
                RLE_ESCAPE_STRUCT,
                decoder=_rle_decode_chunk,
                encoder=None,
            ),
            # Literal: Any byte that is NOT the escape byte
            ExprAdapter(
                FocusedSeq(
                    "val",
                    "val" / Int8ub,
                    "_" / Check(_is_not_esc_byte),
                ),
                decoder=_decode_literal,
                encoder=_encode_literal,
            ),
        )
    ),
    Terminated,
).compile()

# [SIL-2] Public API as a Construct Transformation
RLE_TRANSFORM = RleAdapter(GreedyBytes)


def should_compress(payload: bytes) -> bool:
    """Check if a payload should be RLE compressed."""
    if len(payload) < protocol.RLE_MIN_COMPRESS_INPUT_SIZE:
        return False
    for _, group in itertools.groupby(payload):
        if sum(1 for _ in group) >= protocol.RLE_MIN_RUN_LENGTH:
            return True
    return False


__all__ = ["RLE_TRANSFORM", "should_compress"]
