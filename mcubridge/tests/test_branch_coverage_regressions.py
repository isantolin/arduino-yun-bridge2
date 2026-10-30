"""Regression tests for previously unvisited failure and initialization branches."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

import mcubridge.state.storage as storage_mod
from mcubridge.state.context import RuntimeState


def test_open_lmdb_env_returns_none_after_retry_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent LMDB open failure must be reported, not hidden as a usable DB."""
    open_mock = Mock(side_effect=OSError("storage unavailable"))
    monkeypatch.setattr(storage_mod.lmdb, "open", open_mock)

    env, db = storage_mod._open_lmdb_env(
        str(tmp_path / "unavailable.db"),
        b"test",
        "data.mdb",
    )

    assert open_mock.call_count == 2
    assert env is None
    assert db is None


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
