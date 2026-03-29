"""Protocol encoding helpers for McuBridge.

[SIL-2] Modernised error reporting uses numerical Status codes.
Reason strings are kept for debug purposes but are secondary.
"""

from __future__ import annotations

from mcubridge.protocol import protocol as protocol


def encode_status_reason(reason: str | None) -> bytes:
    """Return a UTF-8 encoded payload trimming to MAX frame limits using native slicing."""
    raw = (reason or "").encode("utf-8", errors="ignore")
    # [SIL-2] Direct slicing delegates truncation to Python's C core
    return raw[: protocol.MAX_PAYLOAD_SIZE]
