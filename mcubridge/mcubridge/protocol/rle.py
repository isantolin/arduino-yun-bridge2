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
from itertools import repeat
from typing import Final

import msgspec
from construct import Struct, Int8ub  # type: ignore

# Escape byte used to signal a run
ESCAPE_BYTE: Final[int] = 0xFF

# Minimum run length to encode (shorter runs are left as literals)
MIN_RUN_LENGTH: Final[int] = 4

# Maximum run length in a single encoded sequence (254 + 2 = 256)
MAX_RUN_LENGTH: Final[int] = 256

# [SIL-2] Declarative RLE Escape Structure: [Escape(B), Count-2(B), Value(B)]
RLE_ESCAPE = Struct(
    "escape" / Int8ub,  # type: ignore
    "count_m2" / Int8ub,  # type: ignore
    "value" / Int8ub,  # type: ignore
)
RLE_ESCAPE_SIZE: Final[int] = 3


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
    # Pattern matches runs of 4-256 same bytes OR any sequence of 0xFF
    for m in re.finditer(b'(.)\\1{3,255}|\\xff+', bytes(data)):
        start, end = m.span()
        result.extend(data[last_end:start])  # Literal gap

        chunk = m.group(0)
        char = chunk[0]
        length = len(chunk)

        if char == ESCAPE_BYTE:
            # All 0xFF must be escaped. Split into chunks of 256 if needed.
            for i in range(0, length, MAX_RUN_LENGTH):
                chunk_len = min(length - i, MAX_RUN_LENGTH)
                result.extend(
                    RLE_ESCAPE.build({  # type: ignore
                        "escape": ESCAPE_BYTE,
                        "count_m2": 255 if chunk_len == 1 else chunk_len - 2,
                        "value": ESCAPE_BYTE,
                    })
                )
        else:
            # Non-0xFF run of 4+ bytes
            result.extend(RLE_ESCAPE.build({  # type: ignore
                "escape": ESCAPE_BYTE,
                "count_m2": length - 2,
                "value": char,
            }))
        last_end = end

    result.extend(data[last_end:])
    return bytes(result)


def decode(data: bytes | bytearray | memoryview) -> bytes:
    """Decode RLE data using regex for fast block copying and construct."""
    if not data:
        return b""

    data_bytes = bytes(data)
    result = bytearray()
    last_end = 0

    # Find all escape sequences (0xFF followed by 2 bytes)
    for m in re.finditer(b'\\xff..', data_bytes, re.DOTALL):
        start, end = m.span()
        # Copy literal data before this escape sequence
        result.extend(data_bytes[last_end:start])

        obj = RLE_ESCAPE.parse(m.group(0))  # type: ignore
        run_len = 1 if obj.count_m2 == 255 else obj.count_m2 + 2  # type: ignore
        result.extend(repeat(obj.value, run_len))  # type: ignore

        last_end = end

    # Check for truncated escape sequence at the end
    if data_bytes.find(b'\xff', last_end) != -1:
         raise ValueError("Malformed RLE: truncated escape sequence")

    # Copy any remaining literal data
    result.extend(data_bytes[last_end:])

    return bytes(result)


def should_compress(data: bytes | bytearray | memoryview) -> bool:
    """Heuristic to decide if compression is beneficial using regex."""
    if len(data) < 8:
        return False

    data_bytes = bytes(data)
    # Savings from runs of non-0xFF bytes (N bytes become 3)
    savings = sum(len(m.group(0)) - 3 for m in re.finditer(b'([^\xff])\\1{3,}', data_bytes))
    # Penalty for 0xFF (each 0xFF costs 2 extra bytes)
    penalty = data_bytes.count(b'\xff') * 2

    return savings > penalty + 4


def compression_ratio(original: bytes, compressed: bytes) -> float:
    """Calculate compression ratio. Ratio > 1 indicates compression."""
    return len(original) / len(compressed) if compressed else 0.0
