"""Extra coverage for mcubridge.daemon."""

import asyncio
from unittest.mock import patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.daemon import BridgeDaemon, SupervisedTaskSpec
from mcubridge.services.handshake import SerialHandshakeFatal


@pytest.mark.asyncio
async def test_daemon_supervise_fatal_exception() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    daemon = BridgeDaemon(config)

    # Task that raises fatal exception
    async def fatal_task():
        raise SerialHandshakeFatal("fatal")

    spec = SupervisedTaskSpec(
        name="test-fatal",
        factory=fatal_task,
        fatal_exceptions=(SerialHandshakeFatal,)
    )

    with pytest.raises(SerialHandshakeFatal):
        await daemon._supervise_task(spec)
    assert daemon.state.supervisor_stats["test-fatal"].fatal is True


@pytest.mark.asyncio
async def test_daemon_supervise_healthy_reset() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    daemon = BridgeDaemon(config)

    # Task that fails immediately, then fails after a long delay (simulating healthy run),
    # causing the supervisor to catch it and reset because it exceeded the healthy threshold (10s).
    call_count = 0
    async def healthy_reset_task():
        nonlocal call_count
        call_count += 1
        # Always fail to force supervisor exception handling
        raise ValueError("fail")

    spec = SupervisedTaskSpec(
        name="test-healthy",
        factory=healthy_reset_task,
        restart_interval=0.1,
        max_restarts=1  # Allow one retry (2 attempts total) then raise
    )

        # Mock time:
        # Use a sequence that increments by a large amount (100s) on every call.
        # This ensures that any duration check `now - prev` where `now` is a subsequent call
        # will yield a large positive value (> 10s), satisfying the "healthy" check.
        # This is robust against the exact number of calls Tenacity makes.
        import itertools
        
        async def task_logic():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ValueError("fail")
            return # Clean exit
    
        spec.factory = task_logic
    
        with (
            patch("asyncio.sleep", return_value=None),
            patch("time.monotonic", side_effect=itertools.count(0, 100.0)),
        ):
            # The task eventually succeeds on the 3rd attempt (after reset).
            # So _supervise_task should return cleanly, NOT raise.
            await daemon._supervise_task(spec)
    
        # It should have a record because it failed previously
        assert "test-healthy" in daemon.state.supervisor_stats
        # It failed twice.
        # mark_supervisor_healthy was called after 2nd failure, resetting backoff.
        assert daemon.state.supervisor_stats["test-healthy"].backoff_seconds == 0.0

@pytest.mark.asyncio
async def test_daemon_supervise_cancelled() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    daemon = BridgeDaemon(config)

    async def hanging_task():
        await asyncio.Event().wait()

    spec = SupervisedTaskSpec(name="cancel", factory=hanging_task)

    task = asyncio.create_task(daemon._supervise_task(spec))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
