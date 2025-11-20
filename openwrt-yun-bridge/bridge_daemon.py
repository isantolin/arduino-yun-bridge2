#!/usr/bin/env python3
"""Thin compatibility wrapper for the YunBridge daemon entrypoint."""
from __future__ import annotations

from yunbridge.daemon import main as _main


def main() -> None:
    _main()


if __name__ == "__main__":
    main()
