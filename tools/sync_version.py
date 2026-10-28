#!/usr/bin/env python3
"""Synchronize package metadata versions from the repository VERSION file."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_PATH = ROOT / "VERSION"

TARGETS: tuple[tuple[str, str, str, int], ...] = (
    ("pyproject.toml", r'version\s*=\s*"[^"]+"', 'version = "{version}"', 1),
    ("mcubridge/mcubridge/__init__.py", r'__version__\s*=\s*"[^"]+"', '__version__ = "{version}"', 1),
    ("mcubridge/Makefile", r"PKG_VERSION:=[^\n]+", "PKG_VERSION:={version}", 0),
    ("mcubridge-gateway/Makefile", r"PKG_VERSION:=[^\n]+", "PKG_VERSION:={version}", 0),
    ("luci-app-mcubridge/Makefile", r"PKG_VERSION:=[^\n]+", "PKG_VERSION:={version}", 0),
    ("mcubridge-library-arduino/library.properties", r"version=[^\n]+", "version={version}", 0),
)


def sync_metadata(version: str, repo_root: Path = ROOT) -> list[Path]:
    """Update package metadata without coupling side effects to code generation."""
    if not version or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", version) is None:
        raise ValueError(f"Invalid ecosystem version: {version!r}")

    changed: list[Path] = []
    for relative_path, pattern, replacement, count in TARGETS:
        target = repo_root / relative_path
        if not target.is_file():
            continue
        original = target.read_text(encoding="utf-8")
        updated, matches = re.subn(pattern, replacement.format(version=version), original, count=count)
        if matches == 0:
            raise RuntimeError(f"Could not find version metadata to update in {target}")
        if updated != original:
            target.write_text(updated, encoding="utf-8")
            changed.append(target)
    return changed


def main() -> int:
    if not VERSION_PATH.is_file():
        print(f"Missing version source: {VERSION_PATH}", file=sys.stderr)
        return 1
    try:
        changed = sync_metadata(VERSION_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Version synchronization failed: {exc}", file=sys.stderr)
        return 1
    for path in changed:
        print(f"Updated {path.relative_to(ROOT)}")
    if not changed:
        print("Package metadata already matches VERSION.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
