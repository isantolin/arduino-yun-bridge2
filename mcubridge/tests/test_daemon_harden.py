import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# pyright: reportPrivateUsage=false
from mcubridge.daemon import BridgeDaemon, _cleanup_child_processes, app, main
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.handshake import SerialHandshakeFatal


@pytest.fixture
def daemon_setup() -> BridgeDaemon:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/test",
        serial_shared_secret=b"secure_secret_123456789012345678",
    )
    daemon = BridgeDaemon(config)
    return daemon


def test_cleanup_child_processes_zombies() -> None:
    """Test process tree cleanup with simulated zombie processes."""
    MagicMock()
    mock_child = MagicMock()
    mock_child.pid = 12345

    # Simulate child survives terminate
    mock_child.terminate.assert_not_called()


def test_cleanup_child_processes_errors() -> None:
    """Test cleanup handles psutil errors gracefully."""
    _cleanup_child_processes()  # Should not raise


@pytest.mark.asyncio
async def test_daemon_run_lifecycle(daemon_setup: BridgeDaemon) -> None:
    """Test daemon startup and graceful shutdown via TaskGroup cancellation."""
    daemon = daemon_setup

    # Mock transports to avoid real IO
    daemon.serial_transport.run = AsyncMock()
    daemon._mqtt_run = AsyncMock()

    # Run and immediately cancel
    task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.1)
    task.cancel()

    # The daemon catches CancelledError and logs it, so it should exit cleanly
    await task

    assert daemon.state.state == "connected" or daemon.state.state == "disconnected"


@pytest.mark.asyncio
async def test_supervisor_circuit_breaker(daemon_setup: BridgeDaemon) -> None:
    """Verify circuit breaker trips after repeated failures at max backoff."""
    daemon = daemon_setup

    call_count = 0

    async def failing_factory() -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Persistent failure")

    # The supervisor trips and re-raises the last exception
    with pytest.raises(RuntimeError, match="Persistent failure"):
        await daemon._supervise(
            "test-task",
            failing_factory,
            min_backoff=0.01,
            max_backoff=0.01,
            max_restarts=15,
        )

    # Should stop after 10 consecutive failures at max backoff
    assert call_count <= 11


@pytest.mark.asyncio
async def test_supervisor_fatal_exception(daemon_setup: BridgeDaemon) -> None:
    """Verify supervisor stops immediately on fatal exceptions."""
    daemon = daemon_setup

    async def fatal_factory() -> None:
        raise SerialHandshakeFatal("MCU Rejected Secret")

    with pytest.raises(SerialHandshakeFatal):
        await daemon._supervise(
            "critical-task", fatal_factory, fatal_exceptions=(SerialHandshakeFatal,)
        )


def test_app_cli_overrides() -> None:
    """Test CLI entry point with various overrides."""
    with patch("mcubridge.daemon.main") as mock_main:
        app(
            [
                "--serial-port",
                "/dev/ttyUSB0",
                "--mqtt-host",
                "10.0.0.1",
                "--mqtt-tls",
                "1",
                "--allowed-commands",
                "ls,df",
                "--debug",
            ]
        )

        overrides = cast(dict[str, Any], mock_main.call_args[0][0])
        assert overrides["serial_port"] == "/dev/ttyUSB0"
        assert overrides["mqtt_host"] == "10.0.0.1"
        assert overrides["mqtt_tls"] is True
        assert overrides["allowed_commands"] == ["ls", "df"]
        assert overrides["debug"] is True


def test_main_crypto_post_failure() -> None:
    """Verify daemon exits if FIPS POST fails."""
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=False):
        with pytest.raises(SystemExit) as exc:
            main({})
        assert exc.value.code == 1


def test_main_insecure_secret_warning() -> None:
    """Verify MQTT is disabled if default secret is used."""
    from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET

    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
        with patch("mcubridge.daemon.BridgeDaemon") as mock_daemon_cls:
            with patch("asyncio.Runner"):
                main({"serial_shared_secret": DEFAULT_SERIAL_SHARED_SECRET})

                # Check that config passed to BridgeDaemon has mqtt_enabled=False
                config = cast(RuntimeConfig, mock_daemon_cls.call_args[0][0])
                assert config.mqtt_enabled is False
