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
        """Compress data using batch construction (High Performance)."""
        if not obj:
            return b""

        # [SIL-2] High Performance: Pre-calculate chunks and build in ONE pass
        # This drastically reduces the overhead of Construct's context initialization.
        chunks: list[Any] = []
        for byte_val, group in itertools.groupby(obj):
            run_len = len(list(group))
            while run_len > 0:
                if byte_val == protocol.RLE_ESCAPE_BYTE:
                    chunk_len = 1
                    chunks.append(
                        {
                            "count_m2": protocol.RLE_SINGLE_ESCAPE_MARKER,
                            "value": byte_val,
                        }
                    )
                elif run_len >= protocol.RLE_MIN_RUN_LENGTH:
                    chunk_len = min(run_len, 256)
                    chunks.append(
                        {
                            "count_m2": chunk_len - protocol.RLE_OFFSET,
                            "value": byte_val,
                        }
                    )
                else:
                    chunk_len = run_len
                    # Literals are stored as raw integers for Int8ub build
                    chunks.extend([byte_val] * chunk_len)
                run_len -= chunk_len
        return RLE_BATCH_BUILDER.build(chunks)


def _rle_decode_chunk(obj: Any, ctx: Any) -> bytes:
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

# [SIL-2] Batch builder for RLE sequences.
RLE_BATCH_BUILDER: Construct = GreedyRange(
    Select(
        # Literal: Int8ub will match if the item is an integer (0-255)
        Int8ub,
        # Escape: Struct will match if the item is a dictionary
        RLE_ESCAPE_STRUCT,
    )
)

# [SIL-2] Optimized Decoder Schema
RLE_DECODER: Construct = FocusedSeq(
    "chunks",
    "chunks"
    / GreedyRange(
        Select(
            # Escape sequence: Try this first. It starts with the escape byte.
            ExprAdapter(
                RLE_ESCAPE_STRUCT,
                decoder=_rle_decode_chunk,
                encoder=lambda obj, ctx: None,
            ),
            # Literal: Any byte that is NOT the escape byte
            ExprAdapter(
                FocusedSeq(
                    "val",
                    "val" / Int8ub,
                    "_" / Check(lambda ctx: ctx.val != protocol.RLE_ESCAPE_BYTE),
                ),
                decoder=lambda obj, ctx: bytes([obj]),
                encoder=lambda obj, ctx: obj[0],
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
