"""Credential file helpers for Yun Bridge."""
from __future__ import annotations

import os
import stat
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


def _enforce_secure_permissions(file_path: Path) -> None:
    """Ensure credentials files are only readable by their owner."""

    if not file_path.is_file():
        raise FileNotFoundError(f"credentials file {file_path} not found")

    stat_result = file_path.stat()
    mode = stat.S_IMODE(stat_result.st_mode)
    if mode & 0o077:
        raise PermissionError(
            "credentials file must not be group or world accessible"
        )

    expected_uid = os.geteuid()
    allowed_uids = {expected_uid}
    if expected_uid != 0:
        allowed_uids.add(0)
    if stat_result.st_uid not in allowed_uids:
        raise PermissionError(
            "credentials file must be owned by root or the bridge user"
        )


def load_credentials_file(path: str | Path) -> Dict[str, str]:
    """Parse a simple KEY=VALUE credentials file."""
    file_path = Path(path)
    _enforce_secure_permissions(file_path)

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


__all__ = ["load_credentials_file", "lookup_credential"]
