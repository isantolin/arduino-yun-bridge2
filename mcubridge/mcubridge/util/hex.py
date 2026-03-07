"""Hexadecimal formatting utilities for binary traffic logging."""

from __future__ import annotations

import logging


def format_hex(data: bytes | bytearray | memoryview) -> str:
    """Formats binary data as a space-separated hex string enclosed in brackets.

    Example: [DE AD BE EF]
    """
    if not data:
        return "[]"

    # [SIL-2] Efficient conversion using native .hex() for deterministic performance.
    return f"[{data.hex(' ').upper()}]"


def log_binary_traffic(logger: logging.Logger, level: int, direction: str, label: str, data: bytes) -> None:
    """Logs binary traffic with a standardized hex format for syslog.

    Format: %s %s: [DE AD BE EF]
    """
    if not logger.isEnabledFor(level):
        return

    logger.log(level, "%s %s: %s", direction, label, format_hex(data))
