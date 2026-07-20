"""Eighth targeted coverage gap closure for runtime.py system actions, version requests,
console queue flushing, and process monitoring to pass 95% total Python coverage. [SIL-2]"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.topics import parse_topic
from mcubridge.services.runtime import BridgeRequest, BridgeService
from mcubridge.state.context import ProcessContext, RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport

# ==============================================================================
# Fixtures
# ==============================================================================


def _make_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
        topic_prefix="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123456abcd",
        file_system_root=str(tmp_path / "fs"),
        cloud_spool_dir=str(tmp_path / "spool"),
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> RuntimeConfig:
    return _make_config(tmp_path)


@pytest.fixture
def state(cfg: RuntimeConfig) -> Iterator[RuntimeState]:
    s = create_runtime_state(cfg)
    yield s
    s.cleanup()


# ==============================================================================
# runtime.py — system actions, version publishing, console flush & process monitor
# ==============================================================================


@pytest.mark.asyncio
async def test_handle_system_actions(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_system covers BOOTLOADER, FREE_MEMORY, VERSION, and BRIDGE (lines 1101-1141)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(cfg, state, mock_serial)

    fn_sys = getattr(service, "_handle_system")

    # 1. BOOTLOADER
    route_bl = parse_topic("br", "br/system/bootloader")
    assert route_bl is not None
    await fn_sys(route_bl, BridgeRequest(topic="br/system/bootloader", payload=b""))
    mock_serial.send.assert_awaited_once()

    # 2. FREE_MEMORY
    route_mem = parse_topic("br", "br/system/free_memory/get")
    assert route_mem is not None
    resp_mem = pb.FreeMemoryResponse(value=2048).SerializeToString()
    mock_serial.send = AsyncMock(return_value=resp_mem)
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_sys(route_mem, BridgeRequest(topic="br/system/free_memory/get", payload=b""))
        mock_enqueue.assert_awaited_once()

    # 3. VERSION
    route_ver = parse_topic("br", "br/system/version/get")
    assert route_ver is not None
    with patch.object(service, "_request_mcu_version", new=AsyncMock()) as mock_ver:
        await fn_sys(route_ver, BridgeRequest(topic="br/system/version/get", payload=b""))
        mock_ver.assert_awaited_once()

    # 4. BRIDGE (summary & handshake flavors)
    route_br_sum = parse_topic("br", "br/system/bridge/summary")
    assert route_br_sum is not None
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_sys(route_br_sum, BridgeRequest(topic="br/system/bridge/summary", payload=b""))
        mock_enqueue.assert_awaited_once()

    route_br_hs = parse_topic("br", "br/system/bridge/handshake")
    assert route_br_hs is not None
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_sys(route_br_hs, BridgeRequest(topic="br/system/bridge/handshake", payload=b""))
        mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_mcu_version(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_request_mcu_version covers send failure & success (lines 1145-1155)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(cfg, state, mock_serial)

    fn_ver = getattr(service, "_request_mcu_version")

    # 1. Failure -> returns False
    mock_serial.send = AsyncMock(return_value=False)
    res = await fn_ver()
    assert res is False

    # 2. Success -> parses version and publishes
    ver_pb = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    mock_serial.send = AsyncMock(return_value=ver_pb)
    with patch.object(service, "_publish_version", new=AsyncMock()) as mock_pub:
        res = await fn_ver()
        assert res is True
        assert state.mcu_version == (2, 8, 5)
        mock_pub.assert_awaited_once()


@pytest.mark.asyncio
async def test_flush_console_queue(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_flush_console_queue flushes console items to serial (lines 1166-1177)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.send = AsyncMock(side_effect=[False])  # First chunk send fails
    service = BridgeService(cfg, state, mock_serial)

    state.console_to_mcu_queue.append(b"hello console")
    fn_flush = getattr(service, "_flush_console_queue")
    await fn_flush()
    # Queue item retained on write failure
    assert len(state.console_to_mcu_queue) == 1

    # Now send succeeds
    mock_serial.send = AsyncMock(return_value=True)
    await fn_flush()
    assert len(state.console_to_mcu_queue) == 0


@pytest.mark.asyncio
async def test_run_process_disallowed_policy_and_oserror(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_run_process covers policy rejection and subprocess exec OSError (lines 1178-1196)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))

    fn_run = getattr(service, "_run_process")

    # 1. Policy disallowed command -> returns 0
    with patch("mcubridge.services.runtime.is_command_allowed", return_value=False):
        pid = await fn_run("rm -rf /")
        assert pid == 0

    # 2. Subprocess creation raises OSError -> returns 0
    with patch("mcubridge.services.runtime.is_command_allowed", return_value=True):
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("exec error")):
            pid = await fn_run("nonexistent_binary_xyz")
            assert pid == 0


@pytest.mark.asyncio
async def test_monitor_process_timeout(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_monitor_process handles process timeout (lines 1198-1210)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))

    ctx = ProcessContext.__new__(ProcessContext)
    ctx.handle = AsyncMock()
    ctx.handle.wait = AsyncMock(side_effect=TimeoutError)
    ctx.exit_code = -1

    state.running_processes[999] = ctx

    with patch.object(service, "_terminate_process", new=AsyncMock(return_value=137)):
        fn_mon = getattr(service, "_monitor_process")
        # Run with short sleep patch
        with patch("asyncio.sleep", new=AsyncMock()):
            await fn_mon(999)
            assert ctx.exit_code == 137
