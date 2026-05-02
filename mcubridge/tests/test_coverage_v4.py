"""Verify CLI help works and covers entry paths."""

from __future__ import annotations

import sys
import pytest
from mcubridge.daemon import main

# pyright: reportPrivateUsage=false


def test_daemon_help(monkeypatch: pytest.MonkeyPatch):
    """Verify that --help does not crash and exits with 0."""
    monkeypatch.setattr(sys, "argv", ["mcubridge", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
