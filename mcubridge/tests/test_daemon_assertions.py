"""Assertive tests for BridgeService orchestration and MQTT handling."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from mcubridge.daemon import app
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService


@pytest.fixture
def runtime_config(tmp_path) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyDummy",
        mqtt_enabled=True,
        metrics_enabled=False,
        watchdog_enabled=False,
        bridge_summary_interval=0.0,
        file_system_root=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_daemon_supervise_retries_on_failure(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, _ = service_stack
    mock_factory = AsyncMock(side_effect=[ValueError("fail"), None])
    await service.supervise("test-task", mock_factory, min_backoff=0.01, max_backoff=0.01)
    assert mock_factory.call_count == 2


@pytest.mark.asyncio
async def test_daemon_mqtt_run_disabled(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, _ = service_stack
    service.config.mqtt_enabled = False
    # Should return immediately without connecting
    with patch("mcubridge.services.runtime.BridgeService.connect_mqtt_session") as mock_connect:
        await service.run_mqtt()
        mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_daemon_run_orchestrates_tasks(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, serial = service_stack

    # We mock the underlying methods to avoid real I/O
    serial.run = AsyncMock()
    service.run_mqtt = AsyncMock()

    async def fail_soon() -> None:
        await asyncio.sleep(0.05)
        raise SerialHandshakeFatal("test fatal")

    serial.run.side_effect = fail_soon

    with pytest.raises(ExceptionGroup):
        await service.run()

    assert serial.run.called
    assert service.run_mqtt.called


def test_main_strict_mode_when_default_secret(tmp_path) -> None:
    # Test that the daemon disables MQTT when the default secret is used
    mock_config = RuntimeConfig(serial_shared_secret=b"failsafe_secret_mode", mqtt_enabled=True, file_system_root=str(tmp_path))

    with patch("mcubridge.daemon.load_runtime_config", return_value=mock_config):
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
            with patch("mcubridge.daemon.BridgeService") as mock_service_class:
                with patch("asyncio.Runner"):
                    app([])

                    assert mock_service_class.called
                    used_config = mock_service_class.call_args[0][0]
                    assert used_config.mqtt_enabled is False


def test_main_aborts_on_crypto_failure(tmp_path) -> None:
    mock_config = RuntimeConfig(file_system_root=str(tmp_path))
    with patch("mcubridge.daemon.load_runtime_config", return_value=mock_config):
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=False):
            with patch("asyncio.Runner"):
                with pytest.raises(SystemExit) as exc:
                    app([])
                assert exc.value.code == 1
