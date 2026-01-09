"""CRC-32 IEEE 802.3 implementation for frame integrity verification.

[SIL-2 COMPLIANCE]
This module provides CRC32 calculation that is bit-identical to the
MCU-side implementation in protocol/crc.cpp. Both use:
- Polynomial: 0xEDB88320 (bit-reversed IEEE 802.3)
- Initial value: 0xFFFFFFFF
- Final XOR: 0xFFFFFFFF

The Python stdlib binascii.crc32() uses the same algorithm.

Example:
    >>> from yunbridge.rpc.crc import crc32_ieee
    >>> crc32_ieee(b"123456789")  # Should return 0xCBF43926
    3421780262
"""

from __future__ import annotations

from binascii import crc32

from yunbridge.rpc.protocol import CRC32_MASK


def crc32_ieee(data: bytes, initial: int = 0) -> int:
    """Compute IEEE 802.3 CRC-32 checksum.

    This function wraps Python's binascii.crc32() and masks the result
    to ensure consistent 32-bit unsigned output.

    Args:
        data: Input bytes to checksum.
        initial: Initial CRC value (default 0, internally converted).

    Returns:
        32-bit unsigned CRC value masked by CRC32_MASK.

    Note:
        The result is bit-identical to crc32_ieee() in protocol/crc.cpp
        on the MCU side.
    """
    return crc32(data, initial) & CRC32_MASK
