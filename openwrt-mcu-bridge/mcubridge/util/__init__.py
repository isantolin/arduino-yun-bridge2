"""General-purpose utilities for McuBridge."""

from __future__ import annotations

import logging

__all__ = [
    "log_hexdump",
]


def log_hexdump(logger_instance: logging.Logger, level: int, label: str, data: bytes) -> None:
    """Log binary data in hexadecimal format using syslog-friendly output.

    Format: [HEXDUMP] %s: %s
    """
    if not logger_instance.isEnabledFor(level):
        return

    hex_str = data.hex(" ").upper()
    logger_instance.log(level, "[HEXDUMP] %s: %s", label, hex_str)
