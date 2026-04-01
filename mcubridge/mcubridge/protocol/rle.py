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

from itertools import groupby
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
                decoder=lambda obj, ctx: bytes([obj.value])  # type: ignore[reportUnknownLambdaType]
                * (
                    1
                    if obj.count_m2 == protocol.RLE_SINGLE_ESCAPE_MARKER  # type: ignore[reportUnknownMemberType]
                    else int(obj.count_m2) + protocol.RLE_OFFSET  # type: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                ),
                encoder=lambda obj, ctx: None,  # type: ignore[reportUnknownLambdaType]
            ),
            # Literal byte (MUST NOT be the escape byte)
            ExprAdapter(
                FocusedSeq(
                    "value",
                    "value" / Int8ub,
                    "_" / Check(lambda ctx: ctx.value != protocol.RLE_ESCAPE_BYTE),  # type: ignore[reportUnknownLambdaType]
                ),
                decoder=lambda obj, ctx: bytes([obj]),  # type: ignore[reportUnknownLambdaType]
                encoder=lambda obj, ctx: obj[0],  # type: ignore[reportUnknownLambdaType]
            ),
        )
    ),
    Terminated,
)


def should_compress(payload: bytes) -> bool:
    """Check if a payload should be RLE compressed."""
    if len(payload) < protocol.RLE_MIN_COMPRESS_INPUT_SIZE:
        return False
    # C-Native check using itertools.groupby
    for _, group in groupby(payload):
        # Check if length of group iterator is >= MIN_RUN_LENGTH.
        if sum(1 for _ in group) >= protocol.RLE_MIN_RUN_LENGTH:
            return True
    return False


def encode(uncompressed: bytes) -> bytes:
    """Compress data using optimized itertools grouping (SIL-2 Native)."""
    if not uncompressed:
        return b""

    compressed = bytearray()

    # [SIL-2] Delegate iteration to Python's C core via groupby
    for byte_val, group in groupby(uncompressed):
        # Convert group to list to get length (iterator is consumed)
        run_length = sum(1 for _ in group)

        # Max RLE chunk size is 256
        while run_length > 0:
            chunk_len = min(run_length, 256)

            if byte_val == protocol.RLE_ESCAPE_BYTE:
                # Escape byte literal
                for _ in range(chunk_len):
                    compressed.extend(
                        RLE_ESCAPE.build({
                            "count_m2": protocol.RLE_SINGLE_ESCAPE_MARKER,
                            "value": protocol.RLE_ESCAPE_BYTE,
                        })
                    )
            elif chunk_len >= protocol.RLE_MIN_RUN_LENGTH:
                # Repeated sequence
                compressed.extend(
                    RLE_ESCAPE.build({
                        "count_m2": chunk_len - protocol.RLE_OFFSET,
                        "value": byte_val,
                    })
                )
            else:
                # Literal bytes
                compressed.extend(bytes([byte_val]) * chunk_len)

            run_length -= chunk_len

    return bytes(compressed)
