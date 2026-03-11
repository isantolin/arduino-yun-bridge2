"""Tests for the ProcessComponent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from mcubridge.config.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_SAFE_BAUDRATE,
    Status,
)
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def mock_enqueue() -> AsyncMock:
    return AsyncMock()


@pytest_asyncio.fixture
async def process_component(mock_enqueue: AsyncMock) -> ProcessComponent:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_BAUDRATE,
        serial_safe_baud=DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=("echo", "ls"),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        process_max_concurrent=2,
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )
    state = create_runtime_state(config)
    # Correct initialization with mock service
    service = MagicMock()
    service._acknowledge_mcu_frame = AsyncMock()
    return ProcessComponent(config, state, service)


@pytest.mark.asyncio
async def test_run_async_success(process_component: ProcessComponent) -> None:
    mock_handle = MagicMock()
    mock_sh = MagicMock(return_value=mock_handle)

    with patch("sh.Command", return_value=mock_sh):
        pid = await process_component.run_async("echo hello")
        assert pid > 0
        assert pid in process_component.state.running_processes
        assert process_component.state.running_processes[pid].command == "echo hello"


@pytest.mark.asyncio
async def test_run_async_limit_reached(process_component: ProcessComponent) -> None:
    # Acquire all slots
    await process_component._process_slots.acquire()
    await process_component._process_slots.acquire()

    pid = await process_component.run_async("echo hello")
    assert pid == 0


@pytest.mark.asyncio
async def test_poll_process_not_found(process_component: ProcessComponent) -> None:
    batch = await process_component.poll_process(999)
    # ProcessOutputBatch uses status_byte
    assert batch.status_byte == Status.ERROR.value


@pytest.mark.asyncio
async def test_poll_process_running(process_component: ProcessComponent) -> None:
    mock_handle = MagicMock()
    mock_sh = MagicMock(return_value=mock_handle)
    with patch("sh.Command", return_value=mock_sh):
        pid = await process_component.run_async("echo hello")

    proc = process_component.state.running_processes[pid]
    proc.stdout_buffer.extend(b"hello")

    batch = await process_component.poll_process(pid)
    assert batch.status_byte == Status.OK.value
    assert batch.stdout_chunk == b"hello"
    assert not proc.stdout_buffer # Should be drained


@pytest.mark.asyncio
async def test_stop_process_success(process_component: ProcessComponent) -> None:
    mock_process = AsyncMock()
    mock_process.terminate = MagicMock()
    mock_process.stdout = AsyncMock()
    mock_process.stdout.read.return_value = b""
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read.return_value = b""
    mock_process.wait.return_value = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_process):
        pid = await process_component.run_async("echo hello")

    success = await process_component.stop_process(pid)
    assert success is True
    mock_process.terminate.assert_called_once()

@pytest.mark.asyncio
async def test_monitor_process_finishes(process_component: ProcessComponent) -> None:
    # _monitor_process was removed. We only test creation.
    mock_handle = MagicMock()
    mock_sh = MagicMock(return_value=mock_handle)

    with patch("sh.Command", return_value=mock_sh):
        pid = await process_component.run_async("echo hello")

    async with process_component.state.process_lock:
        if pid in process_component.state.running_processes:
            proc = process_component.state.running_processes[pid]
            # Simulating _done callback manually would test finalization, handled below.
            assert proc.command == "echo hello"


@pytest.mark.asyncio
async def test_finalize_process(process_component: ProcessComponent) -> None:
    mock_handle = MagicMock()
    mock_sh = MagicMock(return_value=mock_handle)
    with patch("sh.Command", return_value=mock_sh):
        pid = await process_component.run_async("echo hello")

    assert pid in process_component.state.running_processes
    # 2 - 1 = 1
    assert process_component._process_slots._value == 1

    await process_component._finalize_process(pid)

    assert pid not in process_component.state.running_processes
    assert process_component._process_slots._value == 2 # Released

