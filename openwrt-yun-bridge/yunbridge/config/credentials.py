"""Credential file helpers for Yun Bridge."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable


def _parse_line(line: str) -> tuple[str, str] | None:
    striped = line.strip()
    if not striped or striped.startswith("#"):
        return None
    if "=" not in striped:
        return None
    key, value = striped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    return key, value


def load_credentials_file(path: str | Path) -> Dict[str, str]:
    """Parse a simple KEY=VALUE credentials file."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"credentials file {file_path} not found")

    content: Dict[str, str] = {}
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        content[key] = value
    return content


def lookup_credential(
    keys: Iterable[str],
    *,
    credential_map: Dict[str, str],
    environ: Dict[str, str],
    fallback: str | None = None,
) -> str | None:
    """Resolve a credential trying env vars first, then the map."""
    for key in keys:
        env_val = environ.get(key)
        if env_val:
            return env_val.strip()
        map_val = credential_map.get(key)
        if map_val:
            return map_val.strip()
    return fallback.strip() if isinstance(fallback, str) and fallback.strip() else fallback


__all__ = ["load_credentials_file", "lookup_credential"]
