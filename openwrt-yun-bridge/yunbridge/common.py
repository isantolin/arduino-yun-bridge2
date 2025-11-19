"""Utility helpers shared across Yun Bridge packages."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Tuple

from cobs import cobs as _cobs  # type: ignore[import]

from .const import ALLOWED_COMMAND_WILDCARD

_COBC_MODULE = _cobs

DecodeError = getattr(_COBC_MODULE, "DecodeError", ValueError)


def cobs_encode(data: bytes) -> bytes:
    """COBS-encode *data* using the upstream library."""
    return _COBC_MODULE.encode(data)


def cobs_decode(data: bytes) -> bytes:
    """COBS-decode *data* using the upstream library."""
    return _COBC_MODULE.decode(data)


def normalise_allowed_commands(commands: Iterable[str]) -> Tuple[str, ...]:
    """Return a deduplicated, lower-cased allow-list preserving wildcards."""
    seen: set[str] = set()
    normalised: list[str] = []
    for item in commands:
        candidate = item.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered == ALLOWED_COMMAND_WILDCARD:
            return (ALLOWED_COMMAND_WILDCARD,)
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised.append(lowered)
    return tuple(normalised)


__all__: Final[tuple[str, ...]] = (
    "DecodeError",
    "cobs_encode",
    "cobs_decode",
    "normalise_allowed_commands",
)
