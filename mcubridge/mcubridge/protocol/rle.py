"""
RLE (Run-Length Encoding) implementation for MCU Bridge protocol.

Simple compression optimized for embedded systems with minimal RAM.
Uses escape-based encoding compatible with the C++ implementation.

Format:
  - Literal byte (not 0xFF): output as-is
  - Escape sequence (0xFF): followed by count byte, then repeated byte
    - count 0-254: run length = count + 2 (so 2-256 bytes)
    - count 255: special marker meaning exactly 1 byte (for single 0xFF)

Examples:
  0xFF 0x03 0x41 = 'A' repeated 5 times (3+2)
  0xFF 0xFF 0xFF = single 0xFF byte (special case)
  0xFF 0x00 0xFF = two 0xFF bytes

Only encodes runs of 4+ identical bytes (break-even at 3).
"""

from __future__ import annotations

import re
from typing import Final

import msgspec
from construct import (  # type: ignore
    Const,
    ExprAdapter,
    GreedyRange,
    Int8ub,
    Select,
    Struct,
)

from . import protocol

# [SIL-2] Declarative RLE Escape Structure: [Escape(B), Count-2(B), Value(B)]
RLE_ESCAPE: Final = Struct(
    "escape" / Const(protocol.RLE_ESCAPE_BYTE, Int8ub),  # type: ignore
    "count_m2" / Int8ub,  # type: ignore
    "value" / Int8ub,  # type: ignore
)


# [SIL-2] Declarative RLE Decoder: Greedy selection between escape sequences and literals
RLE_DECODER: Final = GreedyRange(
    Select(
        # Escape sequence: [0xFF, count_m2, value]
        ExprAdapter(
            RLE_ESCAPE,
            decoder=lambda obj, ctx: bytes([obj.value]) * (1 if obj.count_m2 == 255 else obj.count_m2 + 2),  # type: ignore
            encoder=lambda obj, ctx: None,  # type: ignore
        ),
        # Literal byte (must be < 0xFF for Select to work correctly with Const)
        ExprAdapter(
            Int8ub,
            decoder=lambda obj, ctx: bytes([obj]),  # type: ignore
            encoder=lambda obj, ctx: obj[0],  # type: ignore
        ),
    )
)  # type: ignore


class RLEPayload(msgspec.Struct, frozen=True):
    """Encapsulates RLE-compressed data with a msgspec-compatible interface (Refactor)."""

    data: bytes

    @classmethod
    def from_uncompressed(cls, data: bytes | bytearray | memoryview) -> "RLEPayload":
        """Create an RLEPayload by compressing the input data."""
        return cls(data=encode(data))

    def decompress(self) -> bytes:
        """Decompress the encapsulated data."""
        return decode(self.data)

    def __len__(self) -> int:
        return len(self.data)


def encode(data: bytes | bytearray | memoryview) -> bytes:
    """Encode data using RLE with construct for sequences."""
    if not data:
        return b""

    result = bytearray()
    last_end = 0

    # Pattern matches runs of 4-256 same bytes OR any sequence of RLE_ESCAPE_BYTE
    # We use a compiled pattern for performance.
    escape_pattern = re.escape(bytes([protocol.RLE_ESCAPE_BYTE]))
    pattern = re.compile(b"(.)\\1{3,255}|" + escape_pattern + b"+")

    for m in pattern.finditer(bytes(data)):
        start, end = m.span()
        result.extend(data[last_end:start])  # Literal gap

        chunk = m.group(0)
        char = chunk[0]
        length = len(chunk)

        if char == protocol.RLE_ESCAPE_BYTE:
            # All 0xFF must be escaped. Split into chunks of 256 if needed.
            for i in range(0, length, protocol.RLE_MAX_RUN_LENGTH):
                chunk_len = min(length - i, protocol.RLE_MAX_RUN_LENGTH)
                result.extend(
                    RLE_ESCAPE.build({  # type: ignore
                        "escape": protocol.RLE_ESCAPE_BYTE,
                        "count_m2": 255 if chunk_len == 1 else chunk_len - 2,
                        "value": protocol.RLE_ESCAPE_BYTE,
                    })
                )
        else:
            # Non-ESCAPE_BYTE run of 4+ bytes
            result.extend(
                RLE_ESCAPE.build({  # type: ignore
                    "escape": protocol.RLE_ESCAPE_BYTE,
                    "count_m2": length - 2,
                    "value": char,
                })
            )
        last_end = end

    result.extend(data[last_end:])
    return bytes(result)


def decode(data: bytes | bytearray | memoryview) -> bytes:
    """Decode RLE data using a fully declarative Construct decoder (Sustitución Drástica)."""
    if not data:
        return b""

    try:
        # Construct GreedyRange returns a list of byte chunks
        chunks: list[bytes] = RLE_DECODER.parse(data)  # type: ignore
        return b"".join(chunks)
    except Exception as e:
        # SIL-2: Deterministic error reporting for malformed streams
        raise ValueError(f"Malformed RLE stream: {e}") from e


def should_compress(data: bytes | bytearray | memoryview) -> bool:
    """Heuristic to decide if compression is beneficial using regex."""
    if len(data) < 8:
        return False

    data_bytes = bytes(data)
    # Savings from runs of non-ESCAPE_BYTE bytes (N bytes become 3)
    # Use f-string or concat to avoid literal \xff in code where possible
    pattern = re.compile(b"([^" + re.escape(bytes([protocol.RLE_ESCAPE_BYTE])) + b"])\\1{3,}")
    savings = sum(len(m.group(0)) - 3 for m in pattern.finditer(data_bytes))

    # Penalty for ESCAPE_BYTE (each ESCAPE_BYTE costs 2 extra bytes)
    penalty = data_bytes.count(protocol.RLE_ESCAPE_BYTE) * 2

    return savings > penalty + 4


def compression_ratio(original: bytes, compressed: bytes) -> float:
    """Calculate compression ratio. Ratio > 1 indicates compression."""
    return len(original) / len(compressed) if compressed else 0.0
