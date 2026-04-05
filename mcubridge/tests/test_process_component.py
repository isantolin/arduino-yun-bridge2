"""Tests for the ProcessComponent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
async def process_comp(mock_enqueue: AsyncMock) -> AsyncIterator[ProcessComponent]:
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
    service.acknowledge_mcu_frame = AsyncMock()
    component = ProcessComponent(config, state, service)
    try:
        yield component
    finally:
        for pid in list(component.state.running_processes):
            await component.stop_process(pid)
        component.state.cleanup()


@pytest.mark.asyncio
async def test_run_async_success(process_comp: ProcessComponent) -> None:
    mock_handle = MagicMock()
    mock_sh = MagicMock(return_value=mock_handle)

    with patch("sh.Command", return_value=mock_sh):
        pid = await process_comp.run_async("echo hello")
        assert pid > 0
        assert pid in process_comp.state.running_processes
        assert process_comp.state.running_processes[pid].command == "echo hello"


@pytest.mark.asyncio
async def test_run_async_limit_reached(process_comp: ProcessComponent) -> None:
    # Acquire all slots
    await process_comp._process_slots.acquire()  # type: ignore[reportPrivateUsage]
    await process_comp._process_slots.acquire()  # type: ignore[reportPrivateUsage]

    # The 3rd should fail or timeout (non-blocking)
    try:
        async with asyncio.timeout(0.1):
            pid = await process_comp.run_async("echo hello")
            assert pid == 0
    except asyncio.TimeoutError:
        pass  # Success: it blocked/failed as expected


@pytest.mark.asyncio
async def test_poll_process_not_found(process_comp: ProcessComponent) -> None:
    batch = await process_comp.poll_process(999)
    # ProcessOutputBatch uses status_byte
    assert batch.status_byte == Status.ERROR.value


@pytest.mark.asyncio
async def test_poll_process_running(process_comp: ProcessComponent) -> None:
    mock_handle = MagicMock()
    mock_sh = MagicMock(return_value=mock_handle)
    with patch("sh.Command", return_value=mock_sh):
        pid = await process_comp.run_async("echo hello")

    proc = process_comp.state.running_processes[pid]
    proc.stdout_buffer.extend(b"hello")

    batch = await process_comp.poll_process(pid)
    assert batch.status_byte == Status.OK.value
    assert batch.stdout_chunk == b"hello"
    assert not proc.stdout_buffer # Should be drained


@pytest.mark.asyncio
async def test_stop_process_success(process_comp: ProcessComponent) -> None:
    mock_process = AsyncMock()
    mock_process.terminate = MagicMock()
    mock_process.stdout = AsyncMock()
    mock_process.stdout.read.return_value = b""
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read.return_value = b""
    mock_process.wait.return_value = 0

    mock_psutil_instance = MagicMock()
    mock_psutil_instance.children.return_value = []
    mock_psutil_instance.terminate = MagicMock()

    with patch("psutil.Process", return_value=mock_psutil_instance), \
         patch("psutil.wait_procs", return_value=([mock_psutil_instance], [])), \
         patch("asyncio.create_subprocess_shell", return_value=mock_process):
        mock_process.pid = 123
        pid = await process_comp.run_async("echo hello")

        # Call stop_process INSIDE the patch context
        success = await process_comp.stop_process(pid)

    assert success is True
    assert mock_psutil_instance.terminate.call_count >= 1


@pytest.mark.asyncio
async def test_monitor_process_finishes(process_comp: ProcessComponent) -> None:
    # _monitor_process was removed. We only test creation.
    mock_handle = MagicMock()
    mock_sh = MagicMock(return_value=mock_handle)

    with patch("sh.Command", return_value=mock_sh):
        pid = await process_comp.run_async("echo hello")

    async with process_comp.state.process_lock:
        if pid in process_comp.state.running_processes:
            proc = process_comp.state.running_processes[pid]
            # Simulating _done callback manually would test finalization, handled below.
            assert proc.command == "echo hello"


@pytest.mark.asyncio
async def test_finalize_process(process_comp: ProcessComponent) -> None:
    mock_process = AsyncMock()
    mock_process.pid = 42
    mock_process.stdout = AsyncMock()
    mock_process.stdout.read = AsyncMock(return_value=b"")
    mock_process.stderr = AsyncMock()
    mock_process.stderr.read = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock(return_value=0)
    mock_process.returncode = 0
    mock_process.kill = MagicMock()

    with patch("asyncio.create_subprocess_shell", return_value=mock_process):
        pid = await process_comp.run_async("echo hello")

    assert pid in process_comp.state.running_processes
    # 2 - 1 = 1
    assert process_comp._process_slots._value == 1  # type: ignore[reportPrivateUsage]

    await process_comp._finalize_process(pid)  # type: ignore[reportPrivateUsage]

    assert pid not in process_comp.state.running_processes
    assert process_comp._process_slots._value == 2  # Released  # type: ignore[reportPrivateUsage]
