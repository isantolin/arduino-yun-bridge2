"""Protocol encoding helpers for McuBridge."""

from __future__ import annotations

from mcubridge.protocol import protocol


def encode_status_reason(reason: str | None) -> bytes:
    """Return a UTF-8 encoded payload trimming to MAX frame limits."""
    if not reason:
        return b""
    payload = reason.encode("utf-8", errors="ignore")
    return payload[: protocol.MAX_PAYLOAD_SIZE]
