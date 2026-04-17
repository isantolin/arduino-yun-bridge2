"""General-purpose utilities for McuBridge."""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "normalise_allowed_commands",
    "chunk_bytes",
]


def chunk_bytes(payload: bytes, chunk_size: int) -> list[bytes]:
    """Split payload into fixed-size chunks using optimized itertools.batched (Python 3.12+)."""
    if not payload:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    # [SIL-2] Delegate chunking to Python's C core for maximum performance
    import itertools

    return [bytes(chunk) for chunk in itertools.batched(payload, chunk_size)]


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
