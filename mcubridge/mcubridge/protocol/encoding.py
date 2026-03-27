"""Protocol encoding helpers for McuBridge.

[SIL-2] Modernised error reporting uses numerical Status codes.
Reason strings are kept for debug purposes but are secondary.
"""

from __future__ import annotations

from mcubridge.protocol import protocol


def encode_status_reason(reason: str | None) -> bytes:
    """Return a UTF-8 encoded payload trimming to MAX frame limits using declarative Construct."""
    raw = (reason or "").encode("utf-8", errors="ignore")
    if not raw:
        return b""

    from construct import Bytes  # type: ignore

    limit = protocol.MAX_PAYLOAD_SIZE
    if len(raw) <= limit:
        return raw

    # [SIL-2] Declarative truncation to protocol limits
    try:
        return Bytes(limit).parse(raw[:limit])  # type: ignore
    except Exception:
        return raw[:limit]
