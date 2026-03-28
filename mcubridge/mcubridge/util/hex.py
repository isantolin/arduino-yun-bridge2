"""Hexadecimal formatting utilities for binary traffic logging."""

from __future__ import annotations

import logging


from typing import Any


def format_hex(data: bytes | bytearray | memoryview[Any]) -> str:
    """Formats binary data as a space-separated hex string enclosed in brackets.

    Example: [DE AD BE EF]
    """
    if not data:
        return "[]"

    # [SIL-2] Efficient conversion using native .hex() for deterministic performance.
    return f"[{data.hex(' ').upper()}]"


def log_binary_traffic(
    logger: logging.Logger,
    level: int,
    direction: str,
    label: str,
    data: bytes | bytearray | memoryview[Any],
    sequence_id: int | None = None,
) -> None:
    """Logs binary traffic with a standardized hex format for syslog.

    Format: [DIR] [SEQ:XXXX] [LABEL]: [DE AD BE EF]
    """
    if not logger.isEnabledFor(level):
        return

    seq_part = f" [SEQ:{sequence_id:04X}]" if sequence_id is not None else ""
    logger.log(level, "[%s]%s [%s]: %s", direction.upper(), seq_part, label.upper(), format_hex(data))


def log_hexdump(logger_instance: logging.Logger, level: int, label: str, data: bytes) -> None:
    """Log binary data in hexadecimal format using professional syslog-friendly output."""
    if not logger_instance.isEnabledFor(level):
        return

    # [SIL-2] Direct .hex() delegation for performance and determinism
    hex_str = data.hex(" ").upper()
    logger_instance.log(level, "[HEXDUMP] %s: %s", label, hex_str)
