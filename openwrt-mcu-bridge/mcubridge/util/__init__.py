from __future__ import annotations

import logging
from collections.abc import Iterable

"""General-purpose utilities for McuBridge."""


__all__ = [
    "parse_bool",
    "normalise_allowed_commands",
    "safe_int",
    "safe_float",
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


_TRUE_STRINGS = frozenset({"1", "yes", "on", "true", "enable", "enabled"})



def parse_bool(value: object) -> bool:
    """Parse a boolean value safely from various types."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    s = str(value).lower().strip()
    return s in _TRUE_STRINGS


def normalise_allowed_commands(commands: Iterable[str]) -> tuple[str, ...]:
    """Return a deduplicated, lower-cased allow-list preserving wildcards."""
    seen: set[str] = set()
    normalised: list[str] = []
    for item in commands:
        candidate = item.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered == "*":
            return ("*",)
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised.append(lowered)
    return tuple(normalised)



def safe_int(value: object, default: int) -> int:
    try:
        return int(float(value))  # type: ignore
    except (ValueError, TypeError):
        return default



def safe_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore
    except (ValueError, TypeError):
        return default
