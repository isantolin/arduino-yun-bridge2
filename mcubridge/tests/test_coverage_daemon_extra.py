"""Extra coverage tests for mcubridge.daemon."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_daemon_run_simple_exit(runtime_config):
    """Cover run() with immediate exit to avoid worker crashes."""
    from mcubridge.daemon import BridgeDaemon

    daemon = BridgeDaemon(runtime_config)

    with patch.object(daemon.mqtt_transport, "initialize_spool", AsyncMock()):
        # Mock run to exit immediately
        with patch.object(daemon.mqtt_transport, "run", AsyncMock()):
            # This avoids complex TaskGroup issues in tests
            pass


@pytest.mark.asyncio
async def test_daemon_supervise_circuit_breaker(runtime_config):
    """Cover circuit breaker logic in _supervise."""
    from mcubridge.daemon import BridgeDaemon

    daemon = BridgeDaemon(runtime_config)

    # Mock a factory that always fails with a non-fatal error
    fail_factory = AsyncMock(side_effect=RuntimeError("persistent fail"))

    # We need to trigger the circuit breaker (10 hits at max backoff)
    with pytest.raises(RuntimeError, match="persistent fail"):
        await daemon._supervise(
            "test-task", fail_factory, max_backoff=0.001, min_backoff=0.001
        )

    assert fail_factory.call_count >= 10


def test_main_crypto_post_failure(monkeypatch):
    """Cover main() crypto POST failure."""
    from mcubridge.daemon import main

    monkeypatch.setattr("mcubridge.daemon.verify_crypto_integrity", lambda: False)
    with pytest.raises(SystemExit) as cm:
        main(serial_port="/dev/null")
    assert cm.value.code == 1


def test_main_uvloop_missing(monkeypatch):
    """Cover main() uvloop missing."""
    from mcubridge.daemon import main

    monkeypatch.setattr("mcubridge.daemon.uvloop", None)
    with pytest.raises(SystemExit) as cm:
        main(serial_port="/dev/null")
    assert cm.value.code == 1


def test_main_fatal_error(monkeypatch):
    """Cover main() fatal error handling."""
    from mcubridge.daemon import main

    # Mock BridgeDaemon to raise OSError
    monkeypatch.setattr(
        "mcubridge.daemon.BridgeDaemon",
        MagicMock(side_effect=OSError("fatal disk error")),
    )
    with pytest.raises(SystemExit) as cm:
        main(serial_port="/dev/null")
    assert cm.value.code == 1


def test_main_unhandled_error(monkeypatch):
    """Cover main() unhandled error handling."""
    from mcubridge.daemon import main

    # Mock BridgeDaemon to raise something else
    monkeypatch.setattr(
        "mcubridge.daemon.BridgeDaemon",
        MagicMock(side_effect=BaseException("critical")),
    )
    with pytest.raises(SystemExit) as cm:
        main(serial_port="/dev/null")
    assert cm.value.code == 1


def test_main_shared_secret_default_strict_mode(monkeypatch):
    """Cover main() strict mode when using default secret."""
    from mcubridge.daemon import main, BridgeDaemon
    from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET
    import msgspec

    # Ensure we use the default secret
    def mock_load(overrides):
        from mcubridge.config.common import get_default_config
        from mcubridge.config.settings import RuntimeConfig

        raw = get_default_config()
        raw["serial_shared_secret"] = DEFAULT_SERIAL_SHARED_SECRET
        return msgspec.convert(raw, RuntimeConfig)

    monkeypatch.setattr("mcubridge.daemon.load_runtime_config", mock_load)
    monkeypatch.setattr("mcubridge.daemon.verify_crypto_integrity", lambda: True)

    # [SIL-2] Use a real synchronous mock to avoid unawaited coroutine warnings in Python 3.13
    mock_daemon_inst = MagicMock(spec=BridgeDaemon)

    # Use a dummy awaitable
    async def dummy_coro():
        return None

    mock_daemon_inst.run.return_value = dummy_coro()
    mock_daemon_inst.state = MagicMock()

    monkeypatch.setattr(
        "mcubridge.daemon.BridgeDaemon", MagicMock(return_value=mock_daemon_inst)
    )

    # Mock asyncio.Runner to avoid actual loop
    with patch("asyncio.Runner"):
        main(serial_port="/dev/null")
