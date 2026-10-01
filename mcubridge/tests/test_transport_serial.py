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
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import PendingCommand

from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
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
        await transport._process_packet(b"\x02encoded")

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
        await transport._process_packet(encoded)

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

        transport.is_negotiating = True
        fut = asyncio.get_running_loop().create_future()
        transport.negotiation_future = fut

        encoded = cobsr.encode(
            build_frame(
                command_id=Command.CMD_SET_BAUDRATE_RESP.value,
                sequence_id=0,
                payload=b"",
            )
        )
        await transport._process_packet(encoded)

        assert await fut
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

        mock_serial = AsyncMock()
        mock_serial.is_open = True

        async def _resolve_neg(_data: bytes) -> None:
            if transport.negotiation_future and not transport.negotiation_future.done():
                transport.negotiation_future.set_result(True)

        mock_serial.write = AsyncMock(side_effect=_resolve_neg)
        transport.serial = mock_serial

        # Create an invalid frame manually
        raw = b"\xff" + b"x" * 20

        def mock_decode_fallback(data: Any) -> bytes:
            return raw

        mocker.patch.object(cobsr, "decode", side_effect=mock_decode_fallback)

        await transport._process_packet(b"\x02encoded")
        assert transport.consecutive_crc_errors == 1
        assert not mock_serial.write.called

        # Second error (threshold reached)
        await transport._process_packet(b"\x02encoded")
        assert transport.consecutive_crc_errors == 0
        assert mock_serial.write.called
        mock_serial.write.assert_awaited_once()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_serial_transport_toggle_dtr_error(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = AsyncMock()
    mock_serial.set_modem_pins.side_effect = OSError("I/O error")
    transport.serial = mock_serial
    toggle_dtr_fn: Callable[[], Awaitable[None]] = transport._toggle_dtr
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
    switch_baud: Callable[[int], None] = transport._switch_local_baudrate
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
    correlate_fn: Callable[[int, bytes], None] = transport._correlate_frame
    correlate_fn(Status.ERROR.value, b"")
    res = await send_task
    assert res is False


@pytest.mark.asyncio
async def test_serial_transport_methods_with_none_serial(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.serial = None

    switch_baud: Callable[[int], None] = transport._switch_local_baudrate
    switch_baud(115200)

    transport.current_command = None
    await transport.reset()

    toggle_dtr: Callable[[], Awaitable[None]] = transport._toggle_dtr
    await toggle_dtr()

    await transport.stop()
    stop_event: asyncio.Event = transport._stop_event
    assert stop_event.is_set()

    runtime_config.serial_baud = runtime_config.serial_safe_baud
    transport.consecutive_crc_errors = runtime_config.serial_fallback_threshold - 1
    fallback_fn: Callable[[], Awaitable[None]] = transport._check_baudrate_fallback
    await fallback_fn()


@pytest.mark.asyncio
async def test_serial_transport_correlate_frame_branches(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    from mcubridge.protocol.protocol import Status

    transport = SerialTransport(runtime_config, runtime_state, None)
    correlate_fn: Callable[[int, bytes], None] = transport._correlate_frame

    curr1 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    transport.current_command = curr1
    ack_pkt = pb.AckPacket(command_id=Command.CMD_ANALOG_WRITE.value)
    correlate_fn(Status.ACK.value, ack_pkt.SerializeToString())
    assert curr1.ack_received is False

    curr2 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    transport.current_command = curr2
    ack_matching = pb.AckPacket(command_id=Command.CMD_DIGITAL_WRITE.value)
    correlate_fn(Status.ACK.value, ack_matching.SerializeToString())
    assert curr2.ack_received is True
    assert curr2.success is None

    curr3 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    transport.current_command = curr3
    correlate_fn(Status.OK.value, b"")
    assert curr3.success is None


@pytest.mark.asyncio
async def test_serial_process_packet_negotiating_non_baud_cmd(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.is_negotiating = True
    fut = asyncio.get_running_loop().create_future()
    transport.negotiation_future = fut

    frame_bytes = build_frame(
        command_id=Command.CMD_GET_VERSION.value,
        payload=b"",
        sequence_id=1,
    )
    encoded = cobsr.encode(frame_bytes)
    proc_packet_fn: Callable[[bytes], Awaitable[None]] = transport._process_packet
    await proc_packet_fn(encoded)
    assert not fut.done()


def test_serial_safe_after_configure_branches(mocker: MockerFixture) -> None:
    import errno
    import termios
    import mcubridge.transport.serial as serial_mod

    _safe_after_configure: Callable[[Any], None] = serial_mod._safe_after_configure

    mock_self = MagicMock()
    mock_self._fileno = None
    mock_orig = mocker.patch(
        "mcubridge.transport.serial._orig_after_configure", side_effect=OSError(errno.EINVAL, "Invalid argument")
    )
    _safe_after_configure(mock_self)

    mock_orig.side_effect = OSError(errno.EACCES, "Permission denied")
    with pytest.raises(OSError, match="Permission denied"):
        _safe_after_configure(mock_self)

    mock_orig.side_effect = None
    mock_orig.return_value = None
    mock_self._fileno = 42
    mocker.patch("termios.tcgetattr", side_effect=termios.error("mock termios failure"))
    _safe_after_configure(mock_self)
    assert mock_self._fileno == 42

    mocker.patch("mcubridge.transport.serial._orig_after_configure", None)
    mock_tcset = mocker.patch("termios.tcsetattr")
    cc_list: list[int] = [0] * 32
    attrs: list[Any] = [0, 0, 0, 0, 0, 0, cc_list]
    mocker.patch("termios.tcgetattr", return_value=attrs)
    _safe_after_configure(mock_self)
    mock_tcset.assert_called_once()
    assert cc_list[termios.VMIN] == 1
    assert cc_list[termios.VTIME] == 0


@pytest.mark.asyncio
async def test_serial_read_loop_branches(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    transport = SerialTransport(runtime_config, runtime_state, AsyncMock())
    mock_serial = AsyncMock()

    stop_event: asyncio.Event = transport._stop_event
    stop_event.set()
    read_loop: Callable[..., Awaitable[None]] = transport._read_loop
    await read_loop(mock_serial)
    mock_serial.readuntil.assert_not_called()

    stop_event.clear()
    mock_serial.readuntil.side_effect = [
        protocol.FRAME_DELIMITER,
        asyncio.CancelledError(),
    ]
    with pytest.raises(asyncio.CancelledError):
        await read_loop(mock_serial)

    assert transport.consecutive_crc_errors == 0


@pytest.mark.asyncio
async def test_serial_transport_connect_exceptions(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    async def _cancel_run() -> None:
        raise asyncio.CancelledError()

    transport_cancel = SerialTransport(runtime_config, runtime_state, AsyncMock(), runner_fn=_cancel_run)
    await transport_cancel.connect()

    async def _fatal_run() -> None:
        raise SerialHandshakeFatal("Fatal")

    transport_fatal = SerialTransport(runtime_config, runtime_state, AsyncMock(), runner_fn=_fatal_run)
    with pytest.raises(SerialHandshakeFatal):
        await transport_fatal.connect()


@pytest.mark.asyncio
async def test_serial_correlate_frame_already_resolved(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, AsyncMock())
    cmd = PendingCommand(command_id=Command.CMD_DIGITAL_WRITE.value)
    cmd.mark_success(b"original")
    transport.current_command = cmd

    correlate: Callable[[int, bytes], None] = transport._correlate_frame
    correlate(protocol.Status.ACK.value, b"new_data")
    assert cmd.response_payload == b"original"


@pytest.mark.asyncio
async def test_serial_transport_edge_branches(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    mocker: MockerFixture,
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, AsyncMock())

    # 1. _correlate_frame with empty ACK payload (line 368->381)
    pending = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Status.ACK.value],
    )
    transport.current_command = pending
    correlate: Callable[[int, bytes], None] = transport._correlate_frame
    correlate(Status.ACK.value, b"")
    assert pending.ack_received is True

    # 2. send() when serial is None (line 434)
    transport.serial = None
    res = await transport.send(Command.CMD_DIGITAL_WRITE.value, b"payload")
    assert res is False

    # 3. send_raw when serial_tx_allowed is clear and wait times out (lines 493-497)
    mock_serial = AsyncMock()
    mock_serial.is_open = True
    transport.serial = mock_serial
    runtime_state.serial_tx_allowed.clear()
    mocker.patch("mcubridge.transport.serial.FLOW_CONTROL_WAIT_TIMEOUT_SECONDS", 0.01)
    res_raw = await transport.send_raw(Command.CMD_DIGITAL_WRITE.value, b"raw_test")
    assert res_raw is True
    runtime_state.serial_tx_allowed.set()

    # 4. _negotiate_baudrate when send_raw fails vs succeeds (lines 543 & 546)
    negotiate: Callable[[int], Awaitable[bool]] = transport._negotiate_baudrate
    mock_serial.write = AsyncMock(side_effect=OSError("Write failed"))
    res_neg_fail = await negotiate(115200)
    assert res_neg_fail is False

    async def _mock_write_and_resolve(_data: bytes) -> None:
        fut = transport.negotiation_future
        if fut and not fut.done():
            fut.set_result(True)

    mock_serial.write = AsyncMock(side_effect=_mock_write_and_resolve)
    res_neg_ok = await negotiate(115200)
    assert res_neg_ok is True

    # 5. send() attempt when completion event fired with success=False, failure_status=None (line 475)
    mock_serial2 = AsyncMock()
    mock_serial2.is_open = True
    transport.serial = mock_serial2
    transport.max_attempts = 1

    async def _resolve_pending_without_status(_data: bytes) -> None:
        curr = transport.current_command
        if curr:
            curr.success = False
            curr.failure_status = None
            curr.completion.set()

    mock_serial2.write = AsyncMock(side_effect=_resolve_pending_without_status)
    res_fail_status = await transport.send(Command.CMD_DIGITAL_WRITE.value, b"test")
    assert res_fail_status is False


@pytest.mark.asyncio
async def test_serial_reader_task_reconnects(mocker: MockerFixture) -> None:
    """Test that reader task re-establishes connection on failure."""
    config = RuntimeConfig(
        serial_port="/dev/test0",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        cloud_host="localhost",
        cloud_port=1883,
        cloud_user=None,
        cloud_pass=None,
        cloud_tls=False,
        cloud_cafile=None,
        cloud_certfile=None,
        cloud_keyfile=None,
        topic_prefix="br",
        allowed_commands=(),
        file_system_root=".tmp_tests/reconnect_fs",
        process_timeout=5,
        reconnect_delay=1,
        serial_shared_secret=b"s_e_c_r_e_t_mock",
        allow_non_tmp_paths=True,
    )
    state = AsyncMock(spec=RuntimeState)
    service = AsyncMock(spec=BridgeService)
    service.on_serial_connected = AsyncMock()
    service.on_serial_disconnected = AsyncMock()
    service.register_serial_sender = MagicMock()

    mock_serial = AsyncMock()
    mock_serial.__aenter__.return_value = mock_serial
    mock_serial.__aexit__.return_value = None
    mock_serial.transport = AsyncMock()
    mock_serial.readuntil.side_effect = [
        asyncio.IncompleteReadError(b"", None),
        asyncio.IncompleteReadError(b"", None),
        asyncio.IncompleteReadError(b"", None),
    ]

    mock_async_serial_cls = MagicMock(return_value=mock_serial)
    sleep_count = 0

    async def mock_sleep_fn(duration: Any) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 100:
            raise RuntimeError("Break Loop")

    mock_sleep = AsyncMock(side_effect=mock_sleep_fn)

    mocker.patch("mcubridge.transport.serial.serialx.AsyncSerial", mock_async_serial_cls)
    mocker.patch("asyncio.sleep", mock_sleep)
    transport = SerialTransport(config, state, service)
    with pytest.raises(RuntimeError, match="Break Loop"):
        await transport.run()

    assert mock_async_serial_cls.call_count >= 2
    assert mock_serial.set_modem_pins.call_count >= 2
    assert service.on_serial_connected.called
    assert service.on_serial_disconnected.called
