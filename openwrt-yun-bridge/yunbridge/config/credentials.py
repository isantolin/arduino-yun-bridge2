"""Credential helpers for Yun Bridge.

The daemon now stores secrets directly in UCI and environment variables.
This module only provides ``lookup_credential`` to consolidate the lookup
order used throughout the codebase.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


def lookup_credential(
    keys: Iterable[str],
    *,
    credential_map: Mapping[str, str],
    environ: Mapping[str, str],
    fallback: str | None = None,
) -> str | None:
    """Resolve a credential trying env vars first, then the supplied map."""

    for key in keys:
        if key in environ:
            env_val = environ.get(key)
            if env_val is None:
                return ""
            return env_val.strip()
        if key in credential_map:
            map_val = credential_map.get(key)
            if map_val is None:
                return ""
            return map_val.strip()

    if isinstance(fallback, str):
        candidate = fallback.strip()
        if candidate:
            return candidate
    return fallback


__all__ = ["lookup_credential"]
