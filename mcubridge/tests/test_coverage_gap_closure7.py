"""Seventh targeted coverage gap closure for runtime.py shell, spi, and pin handlers. [SIL-2]"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.topics import parse_topic
from mcubridge.services.runtime import BridgeRequest, BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
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
# runtime.py — Shell, SPI, and Pin handlers (lines 970-1100)
# ==============================================================================


@pytest.mark.asyncio
async def test_handle_shell_run_async_protobuf_and_plain(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_shell covers RUN_ASYNC protobuf vs plain text and error handling (lines 970-999)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))

    fn_shell = getattr(service, "_handle_shell")
    # 1. Plain text payload
    route = parse_topic("br", "br/sh/run_async")
    assert route is not None
    with patch.object(service, "_run_process", return_value=123):
        with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
            await fn_shell(route, BridgeRequest(topic="br/sh/run_async", payload=b"echo hello"))
            mock_enqueue.assert_awaited_once()

    # 2. Protobuf payload
    pb_payload = pb.ProcessRunAsync(command="uptime").SerializeToString()
    with patch.object(service, "_run_process", return_value=124):
        with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
            req = BridgeRequest(topic="br/sh/run_async", payload=pb_payload)
            setattr(req, "content_type", "application/x-protobuf")
            await fn_shell(route, req)
            mock_enqueue.assert_awaited_once()

    # 3. Exception path in _run_process -> pid 0 response
    with patch.object(service, "_run_process", side_effect=OSError("exec error")):
        with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
            await fn_shell(route, BridgeRequest(topic="br/sh/run_async", payload=b"bad_cmd"))
            mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_shell_poll_and_kill(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_shell covers POLL and KILL (lines 1000-1019)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))

    fn_shell = getattr(service, "_handle_shell")
    # 1. POLL
    route_poll = parse_topic("br", "br/sh/poll/105")
    assert route_poll is not None
    with patch.object(service, "_poll_process", return_value=pb.ProcessPollResponse(status=0)):
        with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
            await fn_shell(route_poll, BridgeRequest(topic="br/sh/poll/105", payload=b""))
            mock_enqueue.assert_awaited_once()

    # 2. KILL
    route_kill = parse_topic("br", "br/sh/kill/105")
    assert route_kill is not None
    with patch.object(service, "_stop_process", new=AsyncMock()) as mock_stop:
        await fn_shell(route_kill, BridgeRequest(topic="br/sh/kill/105", payload=b""))
        mock_stop.assert_awaited_once_with(105)


@pytest.mark.asyncio
async def test_handle_spi_actions(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_spi covers BEGIN, END, CONFIG, and TRANSFER (lines 1020-1055)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(cfg, state, mock_serial)

    fn_spi = getattr(service, "_handle_spi")
    # 1. BEGIN
    route_begin = parse_topic("br", "br/spi/begin")
    assert route_begin is not None
    await fn_spi(route_begin, BridgeRequest(topic="br/spi/begin", payload=b""))
    assert mock_serial.send.call_count == 1

    # 2. END
    route_end = parse_topic("br", "br/spi/end")
    assert route_end is not None
    await fn_spi(route_end, BridgeRequest(topic="br/spi/end", payload=b""))
    assert mock_serial.send.call_count == 2

    # 3. CONFIG valid & corrupt
    route_config = parse_topic("br", "br/spi/config")
    assert route_config is not None
    spi_cfg = pb.SpiConfig(frequency=1000000).SerializeToString()
    await fn_spi(route_config, BridgeRequest(topic="br/spi/config", payload=spi_cfg))
    assert mock_serial.send.call_count == 3
    # Corrupt CONFIG
    await fn_spi(route_config, BridgeRequest(topic="br/spi/config", payload=b"\xff\xff"))

    # 4. TRANSFER returning bytes response
    route_xfer = parse_topic("br", "br/spi/transfer")
    assert route_xfer is not None
    resp_pb = pb.SpiTransferResponse(data=b"spi_out").SerializeToString()
    mock_serial.send = AsyncMock(return_value=resp_pb)
    with patch.object(service, "enqueue_cloud", new=AsyncMock()) as mock_enqueue:
        await fn_spi(route_xfer, BridgeRequest(topic="br/spi/transfer", payload=b"spi_in"))
        mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_pin_actions(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_pin covers MODE, DIGITAL READ, ANALOG READ, and WRITE (lines 1057-1100)."""
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(cfg, state, mock_serial)

    fn_pin = getattr(service, "_handle_pin")
    # 1. MODE
    route_mode = parse_topic("br", "br/d/13/mode")
    assert route_mode is not None
    await fn_pin(route_mode, BridgeRequest(topic="br/d/13/mode", payload=b"1"))
    mock_serial.send.assert_awaited_once()

    # 2. DIGITAL READ
    route_dread = parse_topic("br", "br/d/13/read")
    assert route_dread is not None
    await fn_pin(route_dread, BridgeRequest(topic="br/d/13/read", payload=b""))
    assert len(state.pending_digital_reads) == 1

    # 3. ANALOG READ
    route_aread = parse_topic("br", "br/a/1/read")
    assert route_aread is not None
    await fn_pin(route_aread, BridgeRequest(topic="br/a/1/read", payload=b""))
    assert len(state.pending_analog_reads) == 1

    # 4. DIGITAL WRITE
    route_dwrite = parse_topic("br", "br/d/13")
    assert route_dwrite is not None
    await fn_pin(route_dwrite, BridgeRequest(topic="br/d/13", payload=b"1"))

    # 5. ANALOG WRITE
    route_awrite = parse_topic("br", "br/a/2")
    assert route_awrite is not None
    await fn_pin(route_awrite, BridgeRequest(topic="br/a/2", payload=b"128"))
