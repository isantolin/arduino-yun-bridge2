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

from typing import Final

# Escape byte used to signal a run
ESCAPE_BYTE: Final[int] = 0xFF

# Minimum run length to encode (shorter runs are left as literals)
MIN_RUN_LENGTH: Final[int] = 4

# Maximum run length in a single encoded sequence (254 + 2 = 256)
# Note: 255 is reserved as special marker for single-byte escapes
MAX_RUN_LENGTH: Final[int] = 256


def encode(data: bytes) -> bytes:
    """
    Encode data using RLE.

    Args:
        data: Raw bytes to compress

    Returns:
        RLE-encoded bytes
    """
    if not data:
        return b""

    result = bytearray()
    src_len = len(data)
    src_pos = 0

    while src_pos < src_len:
        current = data[src_pos]

        # Count consecutive identical bytes
        run_len = 1
        while (src_pos + run_len < src_len and
               data[src_pos + run_len] == current and
               run_len < MAX_RUN_LENGTH):
            run_len += 1

        if run_len >= MIN_RUN_LENGTH:
            # Encode as run: ESCAPE, count-2, byte
            result.append(ESCAPE_BYTE)
            result.append(run_len - 2)
            result.append(current)
            src_pos += run_len
        elif current == ESCAPE_BYTE:
            # Escape byte(s) but not enough for MIN_RUN_LENGTH
            # Encode as: ESCAPE, run_len-2, 0xFF
            # For 1 byte: ESCAPE, 255 (special marker), 0xFF = single 0xFF
            # For 2 bytes: ESCAPE, 0, 0xFF = two 0xFF
            # For 3 bytes: ESCAPE, 1, 0xFF = three 0xFF
            if run_len == 1:
                # Special case: single 0xFF uses 255 as marker
                result.append(ESCAPE_BYTE)
                result.append(255)  # Special: means exactly 1
                result.append(ESCAPE_BYTE)
            else:
                result.append(ESCAPE_BYTE)
                result.append(run_len - 2)
                result.append(ESCAPE_BYTE)
            src_pos += run_len
        else:
            # Literal byte
            result.append(current)
            src_pos += 1

    return bytes(result)


def decode(data: bytes) -> bytes:
    """
    Decode RLE-encoded data.

    Args:
        data: RLE-encoded bytes

    Returns:
        Decoded raw bytes

    Raises:
        ValueError: If data is malformed
    """
    if not data:
        return b""

    result = bytearray()
    src_len = len(data)
    src_pos = 0

    while src_pos < src_len:
        current = data[src_pos]
        src_pos += 1

        if current == ESCAPE_BYTE:
            # Encoded run: need at least 2 more bytes
            if src_pos + 2 > src_len:
                raise ValueError(
                    f"Malformed RLE: escape at position {src_pos - 1} "
                    f"but only {src_len - src_pos} bytes remaining"
                )

            count_minus_2 = data[src_pos]
            byte_val = data[src_pos + 1]
            src_pos += 2

            # Special case: 255 means exactly 1 byte (for single 0xFF)
            if count_minus_2 == 255:
                run_len = 1
            else:
                run_len = count_minus_2 + 2
            result.extend([byte_val] * run_len)
        else:
            # Literal byte
            result.append(current)

    return bytes(result)


def should_compress(data: bytes) -> bool:
    """
    Check if compression would be beneficial.

    Quick heuristic: count potential runs without full encoding.
    Returns True if encoding is likely to save space.

    Args:
        data: Raw bytes to analyze

    Returns:
        True if compression is recommended
    """
    if len(data) < 8:
        return False  # Too small to benefit

    potential_savings = 0
    escape_count = 0
    i = 0

    while i < len(data):
        current = data[i]

        if current == ESCAPE_BYTE:
            escape_count += 1
            i += 1
            continue

        # Count run
        run_len = 1
        while i + run_len < len(data) and data[i + run_len] == current:
            run_len += 1

        if run_len >= MIN_RUN_LENGTH:
            # Run of N bytes becomes 3 bytes, saving N-3 bytes
            potential_savings += run_len - 3

        i += run_len

    # Each escape byte in non-run context costs 2 extra bytes
    escape_cost = escape_count * 2

    return potential_savings > escape_cost + 4  # Need meaningful savings


def compression_ratio(original: bytes, compressed: bytes) -> float:
    """
    Calculate compression ratio.

    Args:
        original: Original uncompressed data
        compressed: Compressed data

    Returns:
        Ratio (original_size / compressed_size).
        Values > 1 indicate compression, < 1 indicate expansion.
    """
    if not compressed:
        return 0.0
    return len(original) / len(compressed)
