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
            parsed = RLE_DECODER.parse(obj)
            return b"".join(parsed.chunks)
        except (ConstructError, ValueError, TypeError) as e:
            raise ValueError(f"RLE decode failed: {e}") from e

    def _encode(self, obj: bytes, context: Any, path: Any) -> bytes:
        """Compress data using itertools grouping and declarative chunks."""
        if not obj:
            return b""

        # [SIL-2] Transform raw bytes into a list of declarative chunks
        # This list can then be built using a Construct if we wanted to be 100%
        # declarative, but for performance and clarity, we build chunks directly.
        compressed = bytearray()
        for byte_val, group in itertools.groupby(obj):
            run_len = sum(1 for _ in group)
            while run_len > 0:
                if byte_val == protocol.RLE_ESCAPE_BYTE:
                    chunk_len = 1
                    compressed.extend(
                        RLE_ESCAPE.build(
                            {
                                "count_m2": protocol.RLE_SINGLE_ESCAPE_MARKER,
                                "value": byte_val,
                            }
                        )
                    )
                elif run_len >= protocol.RLE_MIN_RUN_LENGTH:
                    chunk_len = min(run_len, 256)
                    compressed.extend(
                        RLE_ESCAPE.build(
                            {
                                "count_m2": chunk_len - protocol.RLE_OFFSET,
                                "value": byte_val,
                            }
                        )
                    )
                else:
                    chunk_len = run_len
                    compressed.extend(bytes([byte_val]) * chunk_len)
                run_len -= chunk_len
        return bytes(compressed)


def _rle_decode_chunk(obj: Any, ctx: Any) -> bytes:
    """Decode an RLE escape sequence chunk."""
    if obj.count_m2 == protocol.RLE_SINGLE_ESCAPE_MARKER:
        return bytes([protocol.RLE_ESCAPE_BYTE])
    count = int(obj.count_m2) + protocol.RLE_OFFSET
    return bytes([obj.value]) * count


# [SIL-2] Declarative RLE Escape Structure: [Escape(B), Count-2(B), Value(B)]
RLE_ESCAPE: Construct = Struct(
    "escape" / Const(protocol.RLE_ESCAPE_BYTE, Int8ub),
    "count_m2" / Int8ub,
    "value" / Int8ub,
)

def _rle_encode_chunk_nop(obj: Any, ctx: Any) -> None:
    """SIL-2: NOP encoder for RLE escape."""
    return None


def _literal_check(ctx: Any) -> bool:
    """SIL-2: Check if current byte is not the protocol escape byte."""
    return int(ctx.value) != protocol.RLE_ESCAPE_BYTE


def _literal_decode_val(obj: int, ctx: Any) -> bytes:
    """SIL-2: Decode a literal byte into bytes."""
    return bytes([obj])


def _literal_encode_val(obj: bytes, ctx: Any) -> int:
    """SIL-2: Encode bytes into an integer."""
    return int(obj[0])


# [SIL-2] Internal Decoder Schema
RLE_DECODER: Construct = Struct(
    "chunks"
    / GreedyRange(
        Select(
            ExprAdapter(
                RLE_ESCAPE,
                decoder=_rle_decode_chunk,
                encoder=_rle_encode_chunk_nop,
            ),
            # Literal byte (MUST NOT be the escape byte)
            ExprAdapter(
                FocusedSeq(
                    "value",
                    "value" / Int8ub,
                    "_" / Check(_literal_check),
                ),
                decoder=_literal_decode_val,
                encoder=_literal_encode_val,
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


def encode(uncompressed: bytes) -> bytes:
    """Legacy wrapper for RleAdapter.build."""
    return RLE_TRANSFORM.build(uncompressed)


def decode(compressed: bytes) -> bytes:
    """Legacy wrapper for RleAdapter.parse."""
    return RLE_TRANSFORM.parse(compressed)
