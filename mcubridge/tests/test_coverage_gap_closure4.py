"""Fourth targeted coverage gap closure for runtime.py process & cloud handling
and serial.py transport edge cases. [SIL-2]"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import PendingCommand
from mcubridge.services.runtime import BridgeService
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
# runtime.py — Process management & MCU status handling
# ==============================================================================


@pytest.mark.asyncio
async def test_runtime_on_mcu_process_poll_no_serial(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_on_mcu_process_poll returns False when serial is None (line 728)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    service.serial = None
    poll_req = pb.ProcessPoll(pid=100)
    fn = getattr(service, "_on_mcu_process_poll")
    res = await fn(poll_req)
    assert res is False


@pytest.mark.asyncio
async def test_runtime_on_mcu_ack_invalid_payload(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_on_mcu_ack logs error when ACK payload is corrupt bytes (line 763)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    fn = getattr(service, "_on_mcu_ack")
    await fn(1, b"\xff\xff\xff\xff")  # Invalid protobuf ACK -> logs error


@pytest.mark.asyncio
async def test_runtime_handle_mcu_status_error_with_bytes(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_mcu_status handles non-utf8 bytes payload (line 790)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    fn = getattr(service, "_handle_mcu_status")
    await fn(Status.ERROR, 1, b"\xff\xfe\xfd")  # Invalid UTF-8 bytes -> hex representation


@pytest.mark.asyncio
async def test_runtime_handle_mcu_status_generic_response(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_mcu_status decodes pb.GenericResponse from bytes."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    payload = pb.GenericResponse(message="custom_error").SerializeToString()
    fn = getattr(service, "_handle_mcu_status")
    await fn(Status.ERROR, 1, payload)


@pytest.mark.asyncio
async def test_runtime_poll_process_not_found(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_poll_process returns error status when pid is not found (lines 1216-1225)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    fn = getattr(service, "_poll_process")
    resp: pb.ProcessPollResponse = await fn(9999)
    assert resp.status == Status.ERROR.value
    assert resp.finished is True


@pytest.mark.asyncio
async def test_runtime_stop_process_not_found(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_stop_process returns False when pid is not found (lines 1260-1261)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    fn = getattr(service, "_stop_process")
    res = await fn(9999)
    assert res is False


@pytest.mark.asyncio
async def test_runtime_stop_process_oserror(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_stop_process handles ProcessLookupError during termination (lines 1264-1265)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    ctx = ProcessContext.__new__(ProcessContext)
    ctx.handle = MagicMock()
    ctx.handle.pid = 1234
    ctx.handle.returncode = None
    ctx.exit_code = -1

    state.running_processes[1234] = ctx

    with patch.object(service, "_terminate_process", side_effect=ProcessLookupError("no proc")):
        fn = getattr(service, "_stop_process")
        res = await fn(1234)
        assert res is True


@pytest.mark.asyncio
async def test_runtime_terminate_process_already_exited(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_terminate_process returns exit code if process already finished (lines 1270-1271)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    ctx = ProcessContext.__new__(ProcessContext)
    ctx.handle = MagicMock()
    ctx.handle.returncode = 0

    fn = getattr(service, "_terminate_process")
    code = await fn(100, ctx, grace_period=1.0)
    assert code == 0


@pytest.mark.asyncio
async def test_runtime_terminate_process_process_lookup_error(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_terminate_process handles os.killpg raising ProcessLookupError (lines 1274-1275)."""
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    ctx = ProcessContext.__new__(ProcessContext)
    ctx.handle = MagicMock()
    ctx.handle.pid = 555
    ctx.handle.returncode = 137

    with patch("os.killpg", side_effect=ProcessLookupError):
        fn = getattr(service, "_terminate_process")
        code = await fn(555, ctx, grace_period=1.0)
        assert code == 137


@pytest.mark.asyncio
async def test_runtime_cloud_task_disabled(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """run_cloud returns early when cloud_enabled is False (lines 1450-1452)."""
    cfg.cloud_enabled = False
    service = BridgeService(cfg, state, AsyncMock(spec=SerialTransport))
    await service.run_cloud()  # should return immediately without connecting


# ==============================================================================
# serial.py — transport edge cases & error paths
# ==============================================================================


@pytest.mark.asyncio
async def test_serial_toggle_dtr_exception(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_toggle_dtr handles SerialException gracefully (lines 174-175)."""
    transport = SerialTransport(cfg, state, None)
    mock_serial = AsyncMock()
    mock_serial.set_modem_pins = AsyncMock(side_effect=OSError("DTR failed"))
    transport.serial = mock_serial

    fn = getattr(transport, "_toggle_dtr")
    await fn()  # should log error and not raise


@pytest.mark.asyncio
async def test_serial_read_loop_limit_overrun(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_read_loop handles LimitOverrunError (lines 189-191)."""
    transport = SerialTransport(cfg, state, None)
    mock_serial = AsyncMock()

    call_count = 0

    async def _readuntil(sep: bytes) -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise asyncio.LimitOverrunError("overrun", 100)
        raise asyncio.IncompleteReadError(b"", None)

    mock_serial.readuntil = _readuntil
    mock_serial.read = AsyncMock(return_value=b"")

    fn = getattr(transport, "_read_loop")
    await fn(mock_serial)
    assert state.serial_decode_errors == 1


@pytest.mark.asyncio
async def test_serial_correlate_frame_bad_ack_payload(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_correlate_frame handles invalid protobuf ACK payload (lines 262-263)."""
    transport = SerialTransport(cfg, state, None)
    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    transport._current = pending  # type: ignore[attr-defined]

    # Correlate ACK with invalid bytes payload
    fn = getattr(transport, "_correlate_frame")
    fn(Status.ACK.value, b"\xff\xff\xff\xff")
    # pending command is still correlation matched to command_id
    assert pending.ack_received is True
