"""Regression tests for previously unvisited failure and initialization branches."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

import mcubridge.state.storage as storage_mod
from mcubridge.state.context import RuntimeState
from mcubridge.state.storage import LmdbCache


def test_lmdb_cache_survives_persistent_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent LMDB open failure must leave the public cache safely unavailable."""
    open_mock = Mock(side_effect=OSError("storage unavailable"))
    monkeypatch.setattr(storage_mod.lmdb, "open", open_mock)

    cache = LmdbCache(str(tmp_path / "unavailable.db"))

    assert open_mock.call_count == 2
    assert cache.env is None
    assert cache.db is None


def test_runtime_state_restores_explicit_metric_counters() -> None:
    """Explicit persisted counters must be copied into the live metrics object."""
    state = RuntimeState(
        unknown_command_count=7,
        serial_decode_errors=3,
        handshake_attempts=11,
        handshake_successes=8,
        watchdog_beats=19,
    )

    assert state.unknown_command_count == 7
    assert state.serial_decode_errors == 3
    assert state.handshake_attempts == 11
    assert state.handshake_successes == 8
    assert state.watchdog_beats == 19
