"""Protocol encoding helpers for McuBridge.

[SIL-2] Modernised error reporting uses numerical Status codes.
Reason strings are kept for legacy/debug purposes but are secondary.
"""

from __future__ import annotations

from mcubridge.protocol import protocol


def encode_status_reason(reason: str | None) -> bytes:
    """Return a UTF-8 encoded payload trimming to MAX frame limits.
    
    Primary error identification is handled by the Status enum in the frame header.
    This reason string provides additional human-readable context.
    """
    if not reason:
        return b""
    payload = reason.encode("utf-8", errors="ignore")
    return payload[: protocol.MAX_PAYLOAD_SIZE]
