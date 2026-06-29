"""Extra coverage for mcubridge service orchestration."""

from __future__ import annotations
import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from mcubridge.services.runtime import BridgeService
from mcubridge.services.handshake import SerialHandshakeFatal


@pytest.mark.asyncio
async def test_daemon_supervise_fatal_exception(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, _ = service_stack

    # Task that raises fatal exception
    async def fatal_task():
        raise SerialHandshakeFatal("fatal")

    with pytest.raises(SerialHandshakeFatal):
        await service.supervise("test-fatal", fatal_task, fatal_exceptions=(SerialHandshakeFatal,))


@pytest.mark.asyncio
async def test_daemon_supervise_restarts(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, _ = service_stack
    call_state = {"call_count": 0}

    async def failing_task():
        call_state["call_count"] += 1
        if call_state["call_count"] <= 2:
            raise ValueError("fail")
        return  # Clean exit

    with patch("asyncio.sleep", return_value=None):
        # Should restart and eventually return
        await service.supervise("test-restart", failing_task)

    assert call_state["call_count"] == 3
    assert (
        "test-restart" not in service.state.supervisor_stats or not service.state.supervisor_stats["test-restart"].fatal
    )


@pytest.mark.asyncio
async def test_daemon_supervise_cancelled(service_stack: tuple[BridgeService, Any, Any]) -> None:
    service, _, _ = service_stack

    async def hanging_task():
        await asyncio.Event().wait()

    task = asyncio.create_task(service.supervise("cancel", hanging_task))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
