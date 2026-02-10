"""General-purpose utilities for McuBridge."""

from __future__ import annotations

import logging

__all__ = [
    "chunk_bytes",
    "log_hexdump",
]

def chunk_bytes(payload: bytes, chunk_size: int) -> list[bytes]:
    """Split payload into fixed-size chunks."""
    if not payload:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [payload[index : index + chunk_size] for index in range(0, len(payload), chunk_size)]


def log_hexdump(logger_instance: logging.Logger, level: int, label: str, data: bytes) -> None:
    """Log binary data in hexadecimal format using syslog-friendly output.

    Format: [HEXDUMP] %s: %s
    """
    if not logger_instance.isEnabledFor(level):
        return

    hex_str = data.hex(" ").upper()
    logger_instance.log(level, "[HEXDUMP] %s: %s", label, hex_str)
