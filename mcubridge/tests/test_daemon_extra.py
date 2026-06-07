"""Extra coverage for mcubridge.daemon."""

import asyncio
from unittest.mock import patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.daemon import BridgeDaemon
from mcubridge.services.handshake import SerialHandshakeFatal


@pytest.mark.asyncio
async def test_daemon_supervise_fatal_exception() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234", allow_non_tmp_paths=True)
    daemon = BridgeDaemon(config)
    try:
        # Task that raises fatal exception
        async def fatal_task():
            raise SerialHandshakeFatal("fatal")

        with pytest.raises(SerialHandshakeFatal):
            await daemon.supervise("test-fatal", fatal_task, fatal_exceptions=(SerialHandshakeFatal,))
    finally:
        daemon.cleanup()


@pytest.mark.asyncio
async def test_daemon_supervise_restarts() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234", allow_non_tmp_paths=True)
    daemon = BridgeDaemon(config)
    try:
        state = {"call_count": 0}

        async def failing_task():
            state["call_count"] += 1
            if state["call_count"] <= 2:
                raise ValueError("fail")
            return  # Clean exit

        with patch("asyncio.sleep", return_value=None):
            # Should restart and eventually return
            await daemon.supervise("test-restart", failing_task)

        assert state["call_count"] == 3
        assert (
            "test-restart" not in daemon.state.supervisor_stats
            or not daemon.state.supervisor_stats["test-restart"].fatal
        )
    finally:
        daemon.cleanup()


@pytest.mark.asyncio
async def test_daemon_supervise_cancelled() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234", allow_non_tmp_paths=True)
    daemon = BridgeDaemon(config)
    try:

        async def hanging_task():
            await asyncio.Event().wait()

        task = asyncio.create_task(daemon.supervise("cancel", hanging_task))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        daemon.cleanup()


def test_init_check_dependencies_success() -> None:
    import mcubridge

    _check_dependencies = getattr(mcubridge, "_check_dependencies")

    # This should pass in the test environment as we have paho-mqtt 2.x
    _check_dependencies()


def test_init_check_dependencies_failure() -> None:
    import mcubridge

    _check_dependencies = getattr(mcubridge, "_check_dependencies")

    # Mocking paho.mqtt.client without CallbackAPIVersion
    with patch("importlib.import_module") as mock_import:

        import types

        mock_mqtt = types.ModuleType("mqtt_client")
        # No CallbackAPIVersion
        mock_import.return_value = mock_mqtt

        with pytest.raises(SystemExit) as excinfo:
            _check_dependencies()
        assert excinfo.value.code == 1
