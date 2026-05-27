"""Assertive tests for BridgeDaemon orchestration and MQTT handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from mcubridge.daemon import BridgeDaemon, main
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.handshake import SerialHandshakeFatal


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyDummy",
        mqtt_enabled=True,
        metrics_enabled=False,
        watchdog_enabled=False,
        bridge_summary_interval=0.0,
    )

@pytest.mark.asyncio
async def test_daemon_supervise_retries_on_failure(runtime_config: RuntimeConfig) -> None:
    daemon = BridgeDaemon(runtime_config)
    mock_factory = AsyncMock(side_effect=[ValueError("fail"), None])

    await daemon.supervise("test-task", mock_factory, min_backoff=0.01, max_backoff=0.01)

    assert mock_factory.call_count == 2

@pytest.mark.asyncio
async def test_daemon_mqtt_run_disabled(runtime_config: RuntimeConfig) -> None:
    runtime_config.mqtt_enabled = False
    daemon = BridgeDaemon(runtime_config)

    # Should return immediately without connecting
    with patch("mcubridge.daemon.BridgeDaemon._connect_mqtt_session") as mock_connect:
        await daemon._mqtt_run()
        mock_connect.assert_not_called()

@pytest.mark.asyncio
async def test_daemon_run_orchestrates_tasks(runtime_config: RuntimeConfig) -> None:
    daemon = BridgeDaemon(runtime_config)

    # We mock the underlying methods to avoid real I/O
    daemon.serial_transport.run = AsyncMock()
    daemon._mqtt_run = AsyncMock()

    async def fail_soon() -> None:
        print("fail_soon started")
        await asyncio.sleep(0.05)
        print("fail_soon raising")
        raise SerialHandshakeFatal("test fatal")

    daemon.serial_transport.run.side_effect = fail_soon

    print("entering with daemon.run")
    with pytest.raises(ExceptionGroup):
        await daemon.run()

    print("finished daemon.run")

    assert daemon.serial_transport.run.called
    assert daemon._mqtt_run.called

def test_main_strict_mode_when_default_secret() -> None:
    # Test that the daemon disables MQTT when the default secret is used
    mock_config = RuntimeConfig(serial_shared_secret=b"failsafe_secret_mode", mqtt_enabled=True)

    with patch("mcubridge.daemon.load_runtime_config", return_value=mock_config):
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
            with patch("mcubridge.daemon.BridgeDaemon") as mock_daemon_class:
                with patch("asyncio.Runner"):
                    main()

                    assert mock_daemon_class.called
                    used_config = mock_daemon_class.call_args[0][0]
                    assert used_config.mqtt_enabled is False

def test_main_aborts_on_crypto_failure() -> None:
    mock_config = RuntimeConfig()
    with patch("mcubridge.daemon.load_runtime_config", return_value=mock_config):
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=False):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1
