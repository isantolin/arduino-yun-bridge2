"""General-purpose utilities for McuBridge."""

from __future__ import annotations

from collections.abc import Iterable

from .hex import format_hex, log_binary_traffic, log_hexdump

__all__ = [
    "parse_bool",
    "normalise_allowed_commands",
    "chunk_bytes",
    "log_hexdump",
    "format_hex",
    "log_binary_traffic",
]


def chunk_bytes(payload: bytes, chunk_size: int) -> list[bytes]:
    """Split payload into fixed-size chunks using declarative Construct."""
    if not payload:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    from construct import GreedyRange, Bytes  # type: ignore

    # [SIL-2] Declarative chunking: GreedyRange of fixed-size Bytes
    # The last chunk might be smaller, handled by GreedyRange's greediness
    # combined with a manual slice for the remainder to ensure exact match.
    full_chunks_count = len(payload) // chunk_size
    remainder = len(payload) % chunk_size

    try:
        chunks: list[bytes] = list(GreedyRange(Bytes(chunk_size)).parse(payload[: full_chunks_count * chunk_size]))  # type: ignore
        if remainder:
            chunks.append(payload[-remainder:])
        return chunks
    except Exception:
        # Fallback to standard slicing if construct fails for any reason
        return [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]


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
    """Return a deduplicated, lower-cased and sorted allow-list preserving wildcards."""
    import re

    all_tokens: list[str] = []
    for c in commands:
        if not c:
            continue
        # [SIL-2] Robust splitting by common delimiters (comma, space)
        tokens = re.split(r"[, \s]+", c.strip().lower())
        all_tokens.extend(t for t in tokens if t)

    items: set[str] = set(all_tokens)
    return ("*",) if "*" in items else tuple(sorted(list(items)))
