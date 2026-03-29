"""Run-Length Encoding (RLE) logic for MCU Bridge RPC communication.

This module implements a simple but efficient RLE compression/decompression
algorithm designed for low-memory microcontrollers (SIL-2).

Format:
- Escape Byte (0xFD)
- Count-2 (1 byte): How many times the value is repeated (beyond the first 2).
- Value (1 byte): The byte value being repeated.
"""

from __future__ import annotations

import re
from typing import Any
import msgspec
from construct import (
    Check,
    Const,
    Construct,
    ExprAdapter,
    FocusedSeq,
    GreedyRange,
    Int8ub,
    Select,
    Struct,
    Terminated,
)

from . import protocol

# [SIL-2] Declarative RLE Escape Structure: [Escape(B), Count-2(B), Value(B)]
RLE_ESCAPE: Construct = Struct(
    "escape" / Const(protocol.RLE_ESCAPE_BYTE, Int8ub),
    "count_m2" / Int8ub,
    "value" / Int8ub,
)

# [SIL-2] Declarative RLE Decoder: Greedy selection between escape sequences and literals
# It is wrapped in a Struct with Terminated to guarantee complete consumption or raise an error.
RLE_DECODER: Construct = Struct(
    "chunks" / GreedyRange(
        Select(
            ExprAdapter(
                RLE_ESCAPE,
                decoder=lambda obj, ctx: bytes([obj.value]) # type: ignore
                * (
                    1
                    if obj.count_m2 == protocol.RLE_SINGLE_ESCAPE_MARKER # type: ignore
                    else int(obj.count_m2) + protocol.RLE_OFFSET # type: ignore
                ),
                encoder=lambda obj, ctx: None, # type: ignore
            ),
            # Literal byte (MUST NOT be the escape byte)
            ExprAdapter(
                FocusedSeq(
                    "value",
                    "value" / Int8ub,
                    "_" / Check(lambda ctx: ctx.value != protocol.RLE_ESCAPE_BYTE) # type: ignore
                ),
                decoder=lambda obj, ctx: bytes([obj]), # type: ignore
                encoder=lambda obj, ctx: obj[0], # type: ignore
            ),
        )
    ),
    Terminated,
)


def should_compress(payload: bytes) -> bool:
    """Check if a payload should be RLE compressed."""
    if len(payload) < protocol.RLE_MIN_COMPRESS_INPUT_SIZE:
        return False
    # Simple heuristic: at least one sequence of 3+ bytes or many escape bytes
    pattern = re.compile(rb"(.)\1{2,}")
    return bool(pattern.search(payload))


def encode(uncompressed: bytes) -> bytes:
    """Compress data using optimized regex pattern matching (SIL-2)."""
    if not uncompressed:
        return b""

    # [SIL-2] Pattern: Any byte repeated 3+ times, or the escape byte itself
    # We cap at 257 repetitions per chunk (Count-2 = 255)
    pattern = re.compile(
        rb"(.)\1{2,256}|" + re.escape(bytes([protocol.RLE_ESCAPE_BYTE]))
    )
    compressed = bytearray()
    last_pos = 0

    for match in pattern.finditer(uncompressed):
        # 1. Append literal segment before the match
        compressed.extend(uncompressed[last_pos : match.start()])

        # 2. Append RLE chunk
        chunk = match.group(0)
        if len(chunk) == 1:
            # Single escape byte literal
            compressed.extend(
                RLE_ESCAPE.build({
                    "count_m2": protocol.RLE_SINGLE_ESCAPE_MARKER,
                    "value": protocol.RLE_ESCAPE_BYTE,
                })
            )
        else:
            # Repeated sequence
            compressed.extend(
                RLE_ESCAPE.build({
                    "count_m2": len(chunk) - protocol.RLE_OFFSET,
                    "value": chunk[0],
                })
            )
        last_pos = match.end()

    # 3. Append remaining literal tail
    compressed.extend(uncompressed[last_pos:])
    return bytes(compressed)


class RLEPayload(msgspec.Struct, frozen=True):
    """Encapsulates RLE-compressed data with a msgspec-compatible interface (Refactor)."""

    data: bytes

    @classmethod
    def from_uncompressed(cls, uncompressed: bytes) -> "RLEPayload":
        """Factory to create RLEPayload from raw bytes."""
        return cls(data=encode(uncompressed))

    def decode(self) -> bytes:
        """Decompress data using declarative Construct decoder."""
        if not self.data:
            return b""
        try:
            parsed: Any = RLE_DECODER.parse(self.data)
            return b"".join(parsed.chunks)
        except Exception as e:
            # Fallback or raise for protocol integrity
            raise ValueError(f"RLE decompression failed: {e}") from e
