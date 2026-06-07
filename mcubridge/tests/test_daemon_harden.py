import asyncio
from typing import cast, Iterator
from unittest.mock import AsyncMock, patch
import pytest

from mcubridge.daemon import BridgeDaemon, app, main
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.handshake import SerialHandshakeFatal


@pytest.fixture
def daemon_setup() -> Iterator[BridgeDaemon]:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/test",
        serial_shared_secret=b"secure_secret_123456789012345678",
    )
    daemon = BridgeDaemon(config)
    yield daemon
    daemon.cleanup()


@pytest.mark.asyncio
async def test_daemon_run_lifecycle(daemon_setup: BridgeDaemon) -> None:
    """Test daemon startup and graceful shutdown via TaskGroup cancellation."""
    daemon = daemon_setup

    # Mock transports to avoid real IO
    daemon.serial_transport.run = AsyncMock()
    setattr(daemon, "_mqtt_run", AsyncMock())

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
        await daemon.supervise(
            "test-task",
            failing_factory,
            min_backoff=0.01,
            max_backoff=0.01,
            max_restarts=15,
        )

    # The supervisor now retries up to max_restarts + 1 times
    assert call_count == 15


@pytest.mark.asyncio
async def test_supervisor_fatal_exception(daemon_setup: BridgeDaemon) -> None:
    """Verify supervisor stops immediately on fatal exceptions."""
    daemon = daemon_setup

    async def fatal_factory() -> None:
        raise SerialHandshakeFatal("MCU Rejected Secret")

    with pytest.raises(SerialHandshakeFatal):
        await daemon.supervise("critical-task", fatal_factory, fatal_exceptions=(SerialHandshakeFatal,))


def test_app_cli_overrides() -> None:
    """CLI no longer accepts operational overrides; UCI is authoritative."""
    with patch("mcubridge.daemon.main") as mock_main:
        with pytest.raises(SystemExit):
            app(["--serial-port", "/dev/ttyUSB0"])
        mock_main.assert_not_called()


def test_main_crypto_post_failure() -> None:
    """Verify daemon exits if FIPS POST fails."""
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=False):
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


def test_main_insecure_secret_warning() -> None:
    """Verify MQTT is disabled if default secret is used."""
    from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET

    insecure_config = RuntimeConfig(serial_shared_secret=DEFAULT_SERIAL_SHARED_SECRET)
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
        with patch("mcubridge.daemon.load_runtime_config", return_value=insecure_config):
            with patch("mcubridge.daemon.BridgeDaemon") as mock_daemon_cls:
                with patch("asyncio.Runner"):
                    main()

                    # Check that config passed to BridgeDaemon has mqtt_enabled=False
                    config = cast(RuntimeConfig, mock_daemon_cls.call_args[0][0])
                    assert config.mqtt_enabled is False
