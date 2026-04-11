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
from construct.core import ConstructError  # type: ignore[import-untyped]

from . import protocol


def _rle_decode(obj: Any, ctx: Any) -> bytes:
    """Decode an RLE escape sequence into repeated bytes."""
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

# [SIL-2] Declarative RLE Decoder
RLE_DECODER: Construct = Struct(
    "chunks" / GreedyRange(
        Select(
            ExprAdapter(
                RLE_ESCAPE,
                decoder=_rle_decode,
                encoder=lambda obj, ctx: None,  # type: ignore[reportUnknownLambdaType]
            ),
            # Literal byte (MUST NOT be the escape byte)
            ExprAdapter(
                FocusedSeq(
                    "value",
                    "value" / Int8ub,
                    "_" / Check(
                        lambda ctx: ctx.value != protocol.RLE_ESCAPE_BYTE,  # type: ignore[reportUnknownLambdaType]
                    ),
                ),
                decoder=lambda obj, ctx: bytes([obj]),  # type: ignore[reportUnknownLambdaType]
                encoder=lambda obj, ctx: obj[0],  # type: ignore[reportUnknownLambdaType]
            ),
        )
    ),
    Terminated,
).compile()


def should_compress(payload: bytes) -> bool:
    """Check if a payload should be RLE compressed."""
    if len(payload) < protocol.RLE_MIN_COMPRESS_INPUT_SIZE:
        return False

    # [SIL-2] Efficient grouping check using itertools.groupby (C-backed)
    for _, group in itertools.groupby(payload):
        count = sum(1 for _ in group)
        if count >= protocol.RLE_MIN_RUN_LENGTH:
            return True
    return False


def encode(uncompressed: bytes) -> bytes:
    """Compress data using itertools grouping and construct building (SIL-2)."""
    if not uncompressed:
        return b""

    compressed = bytearray()

    # [SIL-2] Delegate run identification to itertools.groupby
    for byte_val, group in itertools.groupby(uncompressed):
        run_len = sum(1 for _ in group)

        while run_len > 0:
            if byte_val == protocol.RLE_ESCAPE_BYTE:
                # Escape bytes are always escaped individually
                chunk_len = 1
                compressed.extend(
                    RLE_ESCAPE.build({
                        "count_m2": protocol.RLE_SINGLE_ESCAPE_MARKER,
                        "value": byte_val,
                    })
                )
            elif run_len >= protocol.RLE_MIN_RUN_LENGTH:
                # Repeated sequence: Encode chunks of up to 256
                chunk_len = min(run_len, 256)
                compressed.extend(
                    RLE_ESCAPE.build({
                        "count_m2": chunk_len - protocol.RLE_OFFSET,
                        "value": byte_val,
                    })
                )
            else:
                # Literal bytes
                chunk_len = run_len
                compressed.extend(bytes([byte_val]) * chunk_len)

            run_len -= chunk_len

    return bytes(compressed)


def decode(compressed: bytes) -> bytes:
    """Decompress RLE data using the declarative decoder."""
    if not compressed:
        return b""
    try:
        obj = RLE_DECODER.parse(compressed)
        return b"".join(obj.chunks)
    except (ConstructError, ValueError, TypeError) as e:
        raise ValueError(f"RLE decode failed: {e}") from e
