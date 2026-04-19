"""General-purpose utilities for McuBridge."""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "normalise_allowed_commands",
]


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
