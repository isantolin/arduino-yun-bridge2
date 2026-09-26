import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cobs import cobsr
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame
from mcubridge.protocol.protocol import Command
from mcubridge.protocol.structures import PendingCommand
from mcubridge.services.runtime import BridgeService
from mcubridge.transport.serial import SerialHandshakeFatal, SerialTransport
from pytest_mock import MockerFixture


def _make_config() -> RuntimeConfig:
    import os
    import time

    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
        topic_prefix="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123",
        file_system_root=fs_root,
        cloud_spool_dir="",
        allow_non_tmp_paths=True,
    )


@pytest.mark.asyncio
async def test_process_packet_crc_mismatch_reports_crc(
    mocker: MockerFixture,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        state.connection_fsm.connect()
        state.connection_fsm.synchronize()
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        transport = SerialTransport(config, state, service)

        # Create an invalid frame manually (e.g. version mismatch to trigger ValueError in parse_frame)
        raw = b"\xff" + b"x" * 20

        def mock_decode(data: Any) -> bytes:
            return raw

        mocker.patch.object(cobsr, "decode", side_effect=mock_decode)

        # Manual call to async method
        await getattr(transport, "_process_packet")(b"\x02encoded")

        assert state.serial_decode_errors == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_success_dispatches() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))

        service.handle_mcu_frame = AsyncMock()

        frame_bytes = build_frame(command_id=Command.CMD_CONSOLE_WRITE.value, sequence_id=0, payload=b"hi")
        encoded = cobsr.encode(frame_bytes)
        transport = SerialTransport(config, state, service)
        await getattr(transport, "_process_packet")(encoded)

        service.handle_mcu_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value, 0, b"hi")
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_negotiation_ack_switches_local_baudrate() -> None:
    config = _make_config()
    config.serial_baud = 230400
    config.serial_safe_baud = 115200
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))

        transport = SerialTransport(config, state, service)

        mock_serial = AsyncMock()
        mock_serial.transport = AsyncMock()
        from serialx import Serial

        mock_serial.transport.serial = MagicMock(spec=Serial)
        mock_serial.transport.serial.baudrate = config.serial_safe_baud

        transport.serial = mock_serial

        setattr(transport, "_negotiating", True)
        setattr(transport, "_negotiation_future", asyncio.get_running_loop().create_future())

        encoded = cobsr.encode(
            build_frame(
                command_id=Command.CMD_SET_BAUDRATE_RESP.value,
                sequence_id=0,
                payload=b"",
            )
        )
        await getattr(transport, "_process_packet")(encoded)

        assert await getattr(transport, "_negotiation_future")
        assert mock_serial.transport.serial.baudrate == config.serial_baud
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_write_frame_debug_logs_unknown_command(
    mocker: MockerFixture,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        import mcubridge.transport.serial

        transport = SerialTransport(config, state, service)
        mock_serial = AsyncMock()
        mock_serial.is_open = True
        transport.serial = mock_serial

        mocker.patch.object(
            mcubridge.transport.serial.logger,
            "is_enabled_for",
            return_value=True,
        )
        seen: dict[str, str] = {}

        def mock_debug(msg: str, *args: Any) -> Any:
            return seen.setdefault("msg", msg % args)

        mocker.patch.object(
            mcubridge.transport.serial.logger,
            "debug",
            side_effect=mock_debug,
        )

        def mock_log(_lvl: int, msg: str, *args: Any) -> Any:
            return seen.setdefault("msg", msg % args)

        mocker.patch.object(
            mcubridge.transport.serial.logger,
            "log",
            side_effect=mock_log,
        )

        ok = await transport.send(0xFE, b"payload")
        assert ok
        assert mock_serial.write.called
        # Check that the command 0xFE is present in the encoded hex string
        assert "fe" in seen.get("msg", "").lower()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_write_frame_returns_false_on_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))

        transport = SerialTransport(config, state, service)
        mock_serial = AsyncMock()
        mock_serial.is_open = True
        mock_serial.write.side_effect = OSError("boom")
        transport.serial = mock_serial

        ok = await transport.send(Command.CMD_CONSOLE_WRITE.value, b"hi")
        assert not ok
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_fallback_triggers_negotiation(
    mocker: MockerFixture,
) -> None:
    config = _make_config()
    config.serial_baud = protocol.DEFAULT_BAUDRATE
    config.serial_safe_baud = 57600
    config.serial_fallback_threshold = 2
    state = create_runtime_state(config)
    try:
        state.connection_fsm.connect()
        state.connection_fsm.synchronize()
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))

        transport = SerialTransport(config, state, service)

        # Mock negotiation method
        setattr(transport, "_negotiate_baudrate", AsyncMock(return_value=True))

        # Create an invalid frame manually
        raw = b"\xff" + b"x" * 20

        def mock_decode_fallback(data: Any) -> bytes:
            return raw

        mocker.patch.object(cobsr, "decode", side_effect=mock_decode_fallback)

        await getattr(transport, "_process_packet")(b"\x02encoded")
        assert getattr(transport, "_consecutive_crc_errors") == 1

        getattr(transport, "_negotiate_baudrate").assert_not_called()

        # Second error (threshold reached)
        await getattr(transport, "_process_packet")(b"\x02encoded")
        assert getattr(transport, "_consecutive_crc_errors") == 0

        getattr(transport, "_negotiate_baudrate").assert_awaited_once_with(57600)
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_serial_transport_toggle_dtr_error(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = AsyncMock()
    mock_serial.set_modem_pins.side_effect = OSError("I/O error")
    transport.serial = mock_serial
    toggle_dtr_fn: Callable[[], Awaitable[None]] = getattr(transport, "_toggle_dtr")
    await toggle_dtr_fn()
    assert mock_serial.set_modem_pins.called


def test_serial_transport_switch_local_baudrate_error(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock()
    mock_inner_serial = MagicMock()
    type(mock_inner_serial).baudrate = property(
        fget=lambda self: 115200,
        fset=MagicMock(side_effect=ValueError("Invalid baud")),
    )
    mock_serial.transport.serial = mock_inner_serial
    transport.serial = mock_serial
    switch_baud: Callable[[int], None] = getattr(transport, "_switch_local_baudrate")
    with pytest.raises(RuntimeError):
        switch_baud(99999999)


@pytest.mark.asyncio
async def test_serial_transport_send_failure_status_code(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    from mcubridge.protocol.protocol import Status

    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = AsyncMock()
    mock_serial.is_open = True
    transport.serial = mock_serial

    send_task = asyncio.create_task(transport.send(Command.CMD_GET_VERSION.value, b""))
    await asyncio.sleep(0.01)
    correlate_fn: Callable[[int, bytes], None] = getattr(transport, "_correlate_frame")
    correlate_fn(Status.ERROR.value, b"")
    res = await send_task
    assert res is False


@pytest.mark.asyncio
async def test_serial_transport_methods_with_none_serial(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.serial = None

    switch_baud: Callable[[int], None] = getattr(transport, "_switch_local_baudrate")
    switch_baud(115200)

    setattr(transport, "_current", None)
    await transport.reset()

    toggle_dtr: Callable[[], Awaitable[None]] = getattr(transport, "_toggle_dtr")
    await toggle_dtr()

    await transport.stop()
    stop_event: asyncio.Event = getattr(transport, "_stop_event")
    assert stop_event.is_set()

    runtime_config.serial_baud = runtime_config.serial_safe_baud
    setattr(transport, "_consecutive_crc_errors", runtime_config.serial_fallback_threshold - 1)
    fallback_fn: Callable[[], Awaitable[None]] = getattr(transport, "_check_baudrate_fallback")
    await fallback_fn()


@pytest.mark.asyncio
async def test_serial_transport_correlate_frame_branches(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    from mcubridge.protocol.protocol import Status

    transport = SerialTransport(runtime_config, runtime_state, None)
    correlate_fn: Callable[[int, bytes], None] = getattr(transport, "_correlate_frame")

    curr1 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    setattr(transport, "_current", curr1)
    ack_pkt = pb.AckPacket(command_id=Command.CMD_ANALOG_WRITE.value)
    correlate_fn(Status.ACK.value, ack_pkt.SerializeToString())
    assert curr1.ack_received is False

    curr2 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    setattr(transport, "_current", curr2)
    ack_matching = pb.AckPacket(command_id=Command.CMD_DIGITAL_WRITE.value)
    correlate_fn(Status.ACK.value, ack_matching.SerializeToString())
    assert curr2.ack_received is True
    assert curr2.success is None

    curr3 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    setattr(transport, "_current", curr3)
    correlate_fn(Status.OK.value, b"")
    assert curr3.success is None


@pytest.mark.asyncio
async def test_serial_process_packet_negotiating_non_baud_cmd(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    setattr(transport, "_negotiating", True)
    fut = asyncio.get_running_loop().create_future()
    setattr(transport, "_negotiation_future", fut)

    frame_bytes = build_frame(
        command_id=Command.CMD_GET_VERSION.value,
        payload=b"",
        sequence_id=1,
    )
    encoded = cobsr.encode(frame_bytes)
    proc_packet_fn: Callable[[bytes], Awaitable[None]] = getattr(transport, "_process_packet")
    await proc_packet_fn(encoded)
    assert not fut.done()


def test_serial_safe_after_configure_branches(mocker: MockerFixture) -> None:
    import errno
    import termios
    import mcubridge.transport.serial as serial_mod

    _safe_after_configure: Callable[[Any], None] = getattr(serial_mod, "_safe_after_configure")

    mock_self = MagicMock()
    mock_self._fileno = None
    mock_orig = mocker.patch(
        "mcubridge.transport.serial._orig_after_configure", side_effect=OSError(errno.EINVAL, "Invalid argument")
    )
    _safe_after_configure(mock_self)

    mock_orig.side_effect = OSError(errno.EACCES, "Permission denied")
    with pytest.raises(OSError):
        _safe_after_configure(mock_self)

    mock_orig.side_effect = None
    mock_orig.return_value = None
    mock_self._fileno = 42
    mocker.patch("termios.tcgetattr", side_effect=termios.error("mock termios failure"))
    _safe_after_configure(mock_self)
    assert mock_self._fileno == 42


@pytest.mark.asyncio
async def test_serial_read_loop_branches(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    transport = SerialTransport(runtime_config, runtime_state, AsyncMock())
    mock_serial = AsyncMock()

    stop_event: asyncio.Event = getattr(transport, "_stop_event")
    stop_event.set()
    read_loop: Callable[..., Awaitable[None]] = getattr(transport, "_read_loop")
    await read_loop(mock_serial)
    mock_serial.readuntil.assert_not_called()

    stop_event.clear()
    mock_serial.readuntil.side_effect = [
        protocol.FRAME_DELIMITER,
        asyncio.CancelledError(),
    ]
    mock_proc = AsyncMock()
    setattr(transport, "_process_packet", mock_proc)
    with pytest.raises(asyncio.CancelledError):
        await read_loop(mock_serial)

    mock_proc.assert_not_awaited()
    assert getattr(transport, "_consecutive_crc_errors") == 0


@pytest.mark.asyncio
async def test_serial_transport_connect_exceptions(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, mocker: MockerFixture
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, AsyncMock())

    mocker.patch.object(transport, "_connect_and_run", side_effect=asyncio.CancelledError())
    await transport.connect()

    mocker.patch.object(transport, "_connect_and_run", side_effect=SerialHandshakeFatal("Fatal"))
    with pytest.raises(SerialHandshakeFatal):
        await transport.connect()


@pytest.mark.asyncio
async def test_serial_correlate_frame_already_resolved(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, AsyncMock())
    cmd = PendingCommand(command_id=Command.CMD_DIGITAL_WRITE.value, sequence_id=1, created_at=0.0)
    cmd.success = True
    transport.pending_commands[1] = cmd

    correlate: Callable[[int, bytes], None] = getattr(transport, "_correlate_frame")
    correlate(protocol.Status.ACK.value, b"")
    assert cmd.success is True
