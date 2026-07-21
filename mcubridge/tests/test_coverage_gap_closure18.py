"""Exhaustive gap closure suite 18 for Python daemon SIL-2 coverage (95%+ target)."""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import tenacity
from mcubridge.config.settings import load_runtime_config
from mcubridge.metrics import publish_bridge_snapshots, publish_metrics
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport


@pytest.mark.asyncio
async def test_metrics_loops_exception_handling():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    enqueue_mock = AsyncMock()

    # 1. publish_metrics when _emit_metrics_snapshot raises OSError
    with patch("mcubridge.metrics._emit_metrics_snapshot", side_effect=OSError("Emit error")):
        t1 = asyncio.create_task(publish_metrics(state, enqueue_mock, interval=5.0, min_interval=0.001))
        await asyncio.sleep(0.01)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1

    # 2. publish_bridge_snapshots when _emit_bridge_snapshot raises OSError in summary & handshake loops
    with patch("mcubridge.metrics._emit_bridge_snapshot", side_effect=OSError("Snapshot error")):
        t2 = asyncio.create_task(
            publish_bridge_snapshots(
                state,
                enqueue_mock,
                summary_interval=5.0,
                handshake_interval=5.0,
                min_interval=0.001,
            )
        )
        await asyncio.sleep(0.01)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2


@pytest.mark.asyncio
async def test_serial_send_and_flow_control_gaps():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_port = AsyncMock()
    st = SerialTransport(cfg, state, mock_port)
    st_any = cast(Any, st)

    # 1. send() when send_raw fails
    with patch.object(st, "send_raw", return_value=False):
        res = await st.send(protocol.Command.CMD_DIGITAL_WRITE.value, pb.DigitalWrite(pin=13, value=1))
        assert res is False

    # 2. send() when pending completion times out
    st.serial = mock_port
    st_any._response_timeout = 0.001

    mock_retry_obj = AsyncMock(side_effect=tenacity.RetryError(MagicMock()))
    with patch.object(st, "send_raw", return_value=True), patch("tenacity.AsyncRetrying", return_value=mock_retry_obj):
        res2 = await st.send(protocol.Command.CMD_DIGITAL_WRITE.value, pb.DigitalWrite(pin=13, value=1))
        assert res2 is False

    # 3. send_raw() when serial_tx_allowed wait times out
    state.serial_tx_allowed.clear()
    with patch("asyncio.timeout", side_effect=TimeoutError()):
        res3 = await st.send_raw(protocol.Command.CMD_CONSOLE_WRITE.value, b"test")
        assert res3 is True

    # 4. send_raw() when state is_synchronized (nonce generation)
    state.serial_tx_allowed.set()
    state.mark_synchronized()
    state.link_session_key = b"12345678901234567890123456789012"
    res4 = await st.send_raw(protocol.Command.CMD_CONSOLE_WRITE.value, b"test")
    assert res4 is True
