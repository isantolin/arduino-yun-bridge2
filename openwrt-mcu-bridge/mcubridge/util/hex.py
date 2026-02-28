"""Hexadecimal formatting utilities for binary traffic logging."""

from __future__ import annotations

import logging
from typing import Iterable


def format_hex(data: bytes | bytearray | memoryview | Iterable[int]) -> str:
    """Formats binary data as a space-separated hex string enclosed in brackets.

    Example: [DE AD BE EF]
    """
    if not data:
        return "[]"

    # Efficient conversion using f-strings
    return f"[{' '.join(f'{b:02X}' for b in data)}]"


def log_binary_traffic(logger: logging.Logger, level: int, direction: str, label: str, data: bytes) -> None:
    """Logs binary traffic with a standardized hex format for syslog.

    Format: %s %s: [DE AD BE EF]
    """
    if not logger.isEnabledFor(level):
        return

    logger.log(level, "%s %s: %s", direction, label, format_hex(data))
