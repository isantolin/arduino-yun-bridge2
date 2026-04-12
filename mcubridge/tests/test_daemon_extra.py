"""Extra coverage for mcubridge.daemon."""

import asyncio
from unittest.mock import patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.daemon import BridgeDaemon
from mcubridge.services.handshake import SerialHandshakeFatal


@pytest.mark.asyncio
async def test_daemon_supervise_fatal_exception() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    daemon = BridgeDaemon(config)
    try:
        # Task that raises fatal exception
        async def fatal_task():
            raise SerialHandshakeFatal("fatal")

        with pytest.raises(SerialHandshakeFatal):
            await daemon._supervise(
                "test-fatal", fatal_task, fatal_exceptions=(SerialHandshakeFatal,)
            )
    finally:
        daemon.state.cleanup()


@pytest.mark.asyncio
async def test_daemon_supervise_restarts() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
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
            await daemon._supervise("test-restart", failing_task)

        assert state["call_count"] == 3
        assert "test-restart" in daemon.state.supervisor_stats
    finally:
        daemon.state.cleanup()


@pytest.mark.asyncio
async def test_daemon_supervise_cancelled() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    daemon = BridgeDaemon(config)
    try:

        async def hanging_task():
            await asyncio.Event().wait()

        task = asyncio.create_task(daemon._supervise("cancel", hanging_task))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        daemon.state.cleanup()
