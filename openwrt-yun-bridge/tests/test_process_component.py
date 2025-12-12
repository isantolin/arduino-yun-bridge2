"""Tests for the ProcessComponent."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_BAUD,
    DEFAULT_STATUS_INTERVAL,
)
from yunbridge.policy import CommandValidationError
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.components.base import BridgeContext
from yunbridge.services.components.process import ProcessComponent, ProcessOutputBatch
from yunbridge.state.context import create_runtime_state


@pytest.fixture
def mock_context() -> AsyncMock:
    ctx = AsyncMock(spec=BridgeContext)

    # Mock schedule_background to just await the coroutine immediately for testing
    async def _schedule(coro):
        await coro

    ctx.schedule_background.side_effect = _schedule
    return ctx


@pytest_asyncio.fixture
async def process_component(mock_context: AsyncMock) -> ProcessComponent:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_SERIAL_BAUD,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=DEFAULT_MQTT_TOPIC,
        allowed_commands=("echo", "ls"),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        process_max_concurrent=2,
        serial_shared_secret=b"testsecret",
    )
    state = create_runtime_state(config)
    return ProcessComponent(config, state, mock_context)


@pytest.mark.asyncio
async def test_handle_run_success(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    # Mock run_sync to return success
    with patch.object(ProcessComponent, "run_sync", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (0, b"stdout", b"stderr", 0)

        # Mock _try_acquire_process_slot to return True
        with patch.object(
            ProcessComponent, "_try_acquire_process_slot", new_callable=AsyncMock
        ) as mock_acquire:
            mock_acquire.return_value = True

            # Mock _build_sync_response
            with patch.object(ProcessComponent, "_build_sync_response") as mock_build:
                mock_build.return_value = b"response_payload"

                await process_component.handle_run(b"echo hello")

                mock_run.assert_awaited_once_with("echo hello")
                mock_context.send_frame.assert_awaited_once_with(
                    Command.CMD_PROCESS_RUN_RESP.value, b"response_payload"
                )


@pytest.mark.asyncio
async def test_handle_run_limit_reached(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    # Mock _try_acquire_process_slot to return False
    with patch.object(
        ProcessComponent, "_try_acquire_process_slot", new_callable=AsyncMock
    ) as mock_acquire:
        mock_acquire.return_value = False

        await process_component.handle_run(b"echo hello")

        mock_context.send_frame.assert_awaited_once()
        args = mock_context.send_frame.call_args[0]
        assert args[0] == Status.ERROR.value
        # Should contain "process_limit_reached" encoded
        assert b"process_limit_reached" in args[1]


@pytest.mark.asyncio
async def test_handle_run_validation_error(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    with patch.object(ProcessComponent, "run_sync", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = CommandValidationError("forbidden")

        with patch.object(
            ProcessComponent, "_try_acquire_process_slot", new_callable=AsyncMock
        ) as mock_acquire:
            mock_acquire.return_value = True

            await process_component.handle_run(b"rm -rf /")

            mock_context.send_frame.assert_awaited_once()
            args = mock_context.send_frame.call_args[0]
            assert args[0] == Status.ERROR.value
            assert b"forbidden" in args[1]


@pytest.mark.asyncio
async def test_handle_run_async_success(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    with patch.object(
        ProcessComponent, "start_async", new_callable=AsyncMock
    ) as mock_start:
        mock_start.return_value = 123

        await process_component.handle_run_async(b"sleep 10")

        mock_start.assert_awaited_once_with("sleep 10")
        mock_context.send_frame.assert_awaited_once_with(
            Command.CMD_PROCESS_RUN_ASYNC_RESP.value, struct.pack(">H", 123)
        )
        # Should also enqueue MQTT message
        mock_context.enqueue_mqtt.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_run_async_failure(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    with patch.object(
        ProcessComponent, "start_async", new_callable=AsyncMock
    ) as mock_start:
        mock_start.return_value = 0xFFFF

        await process_component.handle_run_async(b"fail")

        mock_context.send_frame.assert_awaited_once()
        args = mock_context.send_frame.call_args[0]
        assert args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_handle_poll_success(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    pid = 123
    payload = struct.pack(">H", pid)

    batch = ProcessOutputBatch(
        status_byte=1,  # Running
        exit_code=0,
        stdout_chunk=b"out",
        stderr_chunk=b"err",
        finished=False,
        stdout_truncated=False,
        stderr_truncated=False,
    )

    with patch.object(
        ProcessComponent, "collect_output", new_callable=AsyncMock
    ) as mock_collect:
        mock_collect.return_value = batch
        with patch.object(
            ProcessComponent, "publish_poll_result", new_callable=AsyncMock
        ):
            await process_component.handle_poll(payload)

            mock_collect.assert_awaited_once_with(pid)
            mock_context.send_frame.assert_awaited_once()
            args = mock_context.send_frame.call_args[0]
            assert args[0] == Command.CMD_PROCESS_POLL_RESP.value
            # Verify payload structure roughly
            resp = args[1]
            assert resp[0] == 1  # status
            assert resp[1] == 0  # exit code
            # lengths
            assert b"out" in resp
            assert b"err" in resp


@pytest.mark.asyncio
async def test_handle_poll_malformed(
    process_component: ProcessComponent, mock_context: AsyncMock
) -> None:
    await process_component.handle_poll(b"1")  # Too short

    mock_context.send_frame.assert_awaited_once()
    args = mock_context.send_frame.call_args[0]
    assert args[0] == Command.CMD_PROCESS_POLL_RESP.value
    assert args[1][0] == Status.MALFORMED.value
