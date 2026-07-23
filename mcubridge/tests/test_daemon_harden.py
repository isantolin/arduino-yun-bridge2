import asyncio
from typing import cast, Any
from unittest.mock import AsyncMock, patch
import pytest

from mcubridge.daemon import app
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.runtime import BridgeService
from mcubridge.services.handshake import SerialHandshakeFatal


@pytest.fixture
def service_setup(service_stack: tuple[BridgeService, Any, Any]) -> BridgeService:
    service, _, _ = service_stack
    return service


@pytest.mark.asyncio
async def test_daemon_run_lifecycle(service_setup: BridgeService) -> None:
    """Test daemon startup and graceful shutdown via TaskGroup cancellation."""
    service = service_setup

    # Mock transports to avoid real IO
    if service.serial:
        service.serial.run = AsyncMock()
    service.run_cloud = AsyncMock()

    # Run and immediately cancel
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.1)
    task.cancel()

    # The daemon catches CancelledError and logs it, so it should exit cleanly
    await task
    service.cleanup()
    await asyncio.sleep(0.05)
    assert service.state.state in ("connected", "disconnected", "synchronized")


@pytest.mark.asyncio
async def test_supervisor_circuit_breaker(service_setup: BridgeService) -> None:
    """Verify circuit breaker trips after repeated failures at max backoff."""
    service = service_setup

    call_count = 0

    async def failing_factory() -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Persistent failure")

    # The supervisor trips and re-raises the last exception
    with pytest.raises(RuntimeError, match="Persistent failure"):
        await service.supervise(
            "test-task",
            failing_factory,
            min_backoff=0.01,
            max_backoff=0.01,
            max_restarts=15,
            jitter=0,
        )

    # The supervisor now retries up to max_restarts + 1 times
    assert call_count == 15


@pytest.mark.asyncio
async def test_supervisor_fatal_exception(service_setup: BridgeService) -> None:
    """Verify supervisor stops immediately on fatal exceptions."""
    service = service_setup

    async def fatal_factory() -> None:
        raise SerialHandshakeFatal("MCU Rejected Secret")

    with pytest.raises(SerialHandshakeFatal):
        await service.supervise("critical-task", fatal_factory, fatal_exceptions=(SerialHandshakeFatal,))


def test_main_crypto_post_failure() -> None:
    """Verify daemon exits if FIPS POST fails."""
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=False):
        with patch("asyncio.Runner"):
            with pytest.raises(SystemExit) as exc:
                app([])
        assert exc.value.code == 1


def test_main_insecure_secret_warning() -> None:
    """Verify CLOUD is disabled if default secret is used."""
    from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET

    insecure_config = RuntimeConfig(serial_shared_secret=DEFAULT_SERIAL_SHARED_SECRET)
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
        with patch("mcubridge.daemon.load_runtime_config", return_value=insecure_config):
            with patch("mcubridge.daemon.BridgeService") as mock_service_cls:
                with patch("asyncio.Runner"):
                    app([])

                    # Check that config passed to BridgeService has cloud_enabled=False
                    config = cast(RuntimeConfig, mock_service_cls.call_args[0][0])
                    assert not config.cloud_enabled


def test_main_keyboard_interrupt() -> None:
    """Verify app handles KeyboardInterrupt gracefully."""
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
        with patch("mcubridge.daemon.load_runtime_config", side_effect=KeyboardInterrupt):
            app([])


def test_main_value_error() -> None:
    """Verify app handles ValueError and exits with 1."""
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
        with patch("mcubridge.daemon.load_runtime_config", side_effect=ValueError("invalid config")):
            with pytest.raises(SystemExit) as exc:
                app([])
            assert exc.value.code == 1


def test_main_exception_group_handled() -> None:
    """Verify app handles ExceptionGroup and exits with 1 if exceptions are handled."""
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
        exc_group = ExceptionGroup("group", [ValueError("nested value error")])
        with patch("mcubridge.daemon.load_runtime_config", side_effect=exc_group):
            with pytest.raises(SystemExit) as exc:
                app([])
            assert exc.value.code == 1


def test_main_exception_group_unhandled() -> None:
    """Verify app raises unhandled exception groups."""
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
        exc_group = ExceptionGroup("group", [KeyError("unhandled")])
        with patch("mcubridge.daemon.load_runtime_config", side_effect=exc_group):
            with pytest.raises(ExceptionGroup):
                app([])
