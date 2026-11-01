import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import serial

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pw_hdlc.decode import FrameDecoder
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
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


async def test_feed_data_crc_mismatch_reports_crc() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        state.connection_fsm.connect()
        state.connection_fsm.synchronize()
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        transport = SerialTransport(config, state, service)

        # Invalid HDLC frame with bad FCS
        bad_frame = b"\x7e\x01\x03" + b"x" * 20 + b"\x12\x34\x56\x78\x7e"
        await transport.feed_data(bad_frame)

        assert state.serial_decode_errors == 1
    finally:
        state.cleanup()


async def test_feed_data_success_dispatches() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        service.handle_mcu_frame = AsyncMock()

        encoded = protocol.encode_hdlc_console(b"hi")
        transport = SerialTransport(config, state, service)
        await transport.feed_data(encoded)

        service.handle_mcu_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value, 0, b"hi")
    finally:
        state.cleanup()


async def test_feed_data_negotiation_ack_switches_local_baudrate() -> None:
    config = _make_config()
    config.serial_baud = 230400
    config.serial_safe_baud = 115200
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        transport = SerialTransport(config, state, service)

        import serial

        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.baudrate = config.serial_safe_baud
        transport.serial = mock_serial

        transport.state.connection_fsm.start_negotiating()
        fut = asyncio.get_running_loop().create_future()
        transport.negotiation_future = fut

        encoded = protocol.encode_hdlc_rpc(
            Command.CMD_SET_BAUDRATE_RESP.value,
            b"",
            call_id=0,
        )
        await transport.feed_data(encoded)

        assert await fut
        assert mock_serial.baudrate == config.serial_baud
    finally:
        state.cleanup()


async def test_write_frame_debug_logs_unknown_command(
    mocker: MockerFixture,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        import mcubridge.transport.serial

        transport = SerialTransport(config, state, service)
        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.is_open = True
        mock_serial.write = MagicMock(return_value=10)
        mock_serial.flush = MagicMock()
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


async def test_write_frame_returns_false_on_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))

        transport = SerialTransport(config, state, service)
        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.is_open = True
        mock_serial.write = MagicMock(side_effect=OSError("boom"))
        transport.serial = mock_serial

        ok = await transport.send(Command.CMD_CONSOLE_WRITE.value, b"hi")
        assert not ok
    finally:
        state.cleanup()


async def test_feed_data_fallback_triggers_negotiation(
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

        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.is_open = True

        def _resolve_neg(_data: bytes) -> int:
            if transport.negotiation_future and not transport.negotiation_future.done():
                transport.negotiation_future.set_result(True)
            return len(_data)

        mock_serial.write = MagicMock(side_effect=_resolve_neg)
        mock_serial.flush = MagicMock()
        transport.serial = mock_serial

        bad_frame = b"\x7e\x01\x03" + b"x" * 20 + b"\x12\x34\x56\x78\x7e"

        await transport.feed_data(bad_frame)
        assert transport.consecutive_crc_errors == 1
        assert not mock_serial.write.called

        # Second error (threshold reached)
        await transport.feed_data(bad_frame)
        assert transport.consecutive_crc_errors == 0
        assert mock_serial.write.called
    finally:
        state.cleanup()


async def test_serial_transport_toggle_dtr_error(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: Any
) -> None:
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setattr("time.sleep", MagicMock())
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    type(mock_serial).dtr = PropertyMock(side_effect=OSError("I/O error"))
    transport.serial = mock_serial
    toggle_dtr_fn: Callable[[], Awaitable[None]] = transport.toggle_dtr
    await toggle_dtr_fn()


def test_serial_transport_switch_local_baudrate_error(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    type(mock_serial).baudrate = PropertyMock(side_effect=ValueError("Invalid baud"))
    transport.serial = mock_serial
    switch_baud: Callable[[int], None] = transport.switch_local_baudrate
    with pytest.raises(RuntimeError):
        switch_baud(99999999)


async def test_serial_transport_send_failure_status_code(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    from mcubridge.protocol.protocol import Status

    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.write = MagicMock(return_value=10)
    mock_serial.flush = MagicMock()
    transport.serial = mock_serial

    send_task = asyncio.create_task(transport.send(Command.CMD_GET_VERSION.value, b""))
    await asyncio.sleep(0.01)
    correlate_fn: Callable[[int, bytes], None] = transport.correlate_frame
    correlate_fn(Status.ERROR.value, b"")
    res = await send_task
    assert res is False


async def test_serial_transport_methods_with_none_serial(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.serial = None

    switch_baud: Callable[[int], None] = transport.switch_local_baudrate
    switch_baud(115200)

    transport.current_command = None
    await transport.reset()

    toggle_dtr: Callable[[], Awaitable[None]] = transport.toggle_dtr
    await toggle_dtr()

    await transport.stop()
    stop_event: asyncio.Event = transport.stop_event
    assert stop_event.is_set()

    runtime_config.serial_baud = runtime_config.serial_safe_baud
    transport.consecutive_crc_errors = runtime_config.serial_fallback_threshold - 1
    fallback_fn: Callable[[], Awaitable[None]] = transport.check_baudrate_fallback
    await fallback_fn()


async def test_serial_transport_correlate_frame_branches(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    from mcubridge.protocol.protocol import Status

    transport = SerialTransport(runtime_config, runtime_state, None)
    correlate_fn: Callable[[int, bytes], None] = transport.correlate_frame

    # 1. Matching command ID resolves pending command
    curr1 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    transport.current_command = curr1
    correlate_fn(Command.CMD_DIGITAL_WRITE.value, b"data")
    assert curr1.success is True
    assert curr1.response_payload == b"data"

    # 2. Failure status code marks failure
    curr2 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    transport.current_command = curr2
    correlate_fn(Status.ERROR.value, b"")
    assert curr2.success is False

    # 3. Status.OK without expected_resp_ids marks success
    curr3 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[],
    )
    transport.current_command = curr3
    correlate_fn(Status.OK.value, b"ok")
    assert curr3.success is True
    assert curr3.response_payload == b"ok"


async def test_serial_feed_data_negotiating_non_baud_cmd(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.state.connection_fsm.start_negotiating()
    fut = asyncio.get_running_loop().create_future()
    transport.negotiation_future = fut

    encoded = protocol.encode_hdlc_rpc(
        Command.CMD_GET_VERSION.value,
        b"",
        call_id=1,
    )
    feed_fn: Callable[[bytes], Awaitable[None]] = transport.feed_data
    await feed_fn(encoded)
    assert not fut.done()


async def test_serial_read_loop_branches(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    transport = SerialTransport(runtime_config, runtime_state, AsyncMock())
    mock_serial = MagicMock(spec=serial.Serial)

    stop_event: asyncio.Event = transport.stop_event
    stop_event.set()
    read_loop: Callable[..., Awaitable[None]] = transport.read_loop
    await read_loop(mock_serial)
    mock_serial.read_until.assert_not_called()

    # Test OSError during active read loop
    stop_event.clear()
    mock_serial.read_until.side_effect = OSError(5, "Input/output error")
    await read_loop(mock_serial)

    # Test OSError when stopped concurrently during shutdown
    def _readuntil_during_stop(*_args: Any, **_kwargs: Any) -> Any:
        stop_event.set()
        raise OSError(5, "device disconnected")

    stop_event.clear()
    mock_serial.read_until.side_effect = _readuntil_during_stop
    await read_loop(mock_serial)
    assert stop_event.is_set()

    assert transport.consecutive_crc_errors == 0


async def test_serial_transport_connect_exceptions(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    async def _cancel_run() -> None:
        raise asyncio.CancelledError()

    transport_cancel = SerialTransport(runtime_config, runtime_state, AsyncMock())
    transport_cancel.connect_and_run = _cancel_run
    await transport_cancel.run()

    async def _fatal_run() -> None:
        raise SerialHandshakeFatal("Fatal")

    transport_fatal = SerialTransport(runtime_config, runtime_state, AsyncMock())
    transport_fatal.connect_and_run = _fatal_run
    with pytest.raises(SerialHandshakeFatal):
        await transport_fatal.run()


async def test_serial_correlate_frame_already_resolved(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, AsyncMock())
    cmd = PendingCommand(command_id=Command.CMD_DIGITAL_WRITE.value)
    cmd.mark_success(b"original")
    transport.current_command = cmd

    correlate: Callable[[int, bytes], None] = transport.correlate_frame
    correlate(protocol.Status.OK.value, b"new_data")
    assert cmd.response_payload == b"original"


async def test_serial_transport_edge_branches(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    mocker: MockerFixture,
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, AsyncMock())

    # 1. correlate_frame with Status.OK when expected_resp_ids is empty
    pending = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[],
    )
    transport.current_command = pending
    correlate: Callable[[int, bytes], None] = transport.correlate_frame
    correlate(Status.OK.value, b"")
    assert pending.success is True

    # 2. send() when serial is None (line 434)
    transport.serial = None
    res = await transport.send(Command.CMD_DIGITAL_WRITE.value, b"payload")
    assert res is False

    # 3. send_raw when serial_tx_allowed is clear and wait times out
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.write = MagicMock(return_value=10)
    mock_serial.flush = MagicMock()
    transport.serial = mock_serial
    runtime_state.serial_tx_allowed.clear()
    mocker.patch("mcubridge.transport.serial.FLOW_CONTROL_WAIT_TIMEOUT_SECONDS", 0.01)
    res_raw = await transport.send_raw(Command.CMD_DIGITAL_WRITE.value, b"raw_test")
    assert res_raw is True
    runtime_state.serial_tx_allowed.set()

    # 4. negotiate_baudrate when send_raw fails vs succeeds
    negotiate: Callable[[int], Awaitable[bool]] = transport.negotiate_baudrate
    mock_serial.write = MagicMock(side_effect=OSError("Write failed"))
    res_neg_fail = await negotiate(115200)
    assert res_neg_fail is False

    loop = asyncio.get_running_loop()

    def _mock_write_and_resolve(_data: bytes) -> int:
        fut = transport.negotiation_future
        if fut and not fut.done():
            loop.call_soon_threadsafe(fut.set_result, True)
        return len(_data)

    mock_serial.write = MagicMock(side_effect=_mock_write_and_resolve)
    res_neg_ok = await negotiate(115200)
    assert res_neg_ok is True

    # 5. send() attempt when completion event fired with success=False, failure_status=None
    mock_serial2 = MagicMock(spec=serial.Serial)
    mock_serial2.is_open = True
    mock_serial2.flush = MagicMock()
    transport.serial = mock_serial2
    transport.max_attempts = 1

    def _resolve_pending_without_status(_data: bytes) -> int:
        curr = transport.current_command
        if curr:
            curr.success = False
            curr.failure_status = None
            loop.call_soon_threadsafe(curr.completion.set)
        return len(_data)

    mock_serial2.write = MagicMock(side_effect=_resolve_pending_without_status)
    res_fail_status = await transport.send(Command.CMD_DIGITAL_WRITE.value, b"test")
    assert res_fail_status is False


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

    import serial
    from serial import SerialException

    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.read_until.side_effect = SerialException("Connection lost")
    mock_serial.dtr = True
    mock_serial.baudrate = 115200

    mock_serial_for_url = MagicMock(return_value=mock_serial)
    sleep_count = 0

    async def mock_sleep_fn(duration: Any) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 10:
            raise RuntimeError("Break Loop")

    mock_sleep = AsyncMock(side_effect=mock_sleep_fn)

    mocker.patch("serial.serial_for_url", mock_serial_for_url)
    mocker.patch("asyncio.sleep", mock_sleep)
    mocker.patch("time.sleep", MagicMock())
    transport = SerialTransport(config, state, service)
    with pytest.raises(RuntimeError, match="Break Loop"):
        await transport.run()

    assert mock_serial_for_url.call_count >= 2
    assert service.on_serial_connected.called
    assert service.on_serial_disconnected.called


@given(raw_bytes=st.binary(min_size=0, max_size=256))
async def test_feed_data_fuzz_resilience_property(raw_bytes: bytes) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        transport = SerialTransport(config, state, service)

        # Fuzz feed_data with arbitrary bytes: must never raise unhandled exceptions
        await transport.feed_data(raw_bytes)
        assert state.serial_decode_errors >= 0
    finally:
        state.cleanup()


@given(
    command_id=st.integers(1, 0x00FF),
    seq_id=st.integers(1, protocol.UINT16_MAX),
    payload=st.binary(max_size=128),
)
async def test_send_raw_arbitrary_payload_and_sequence_property(
    command_id: int,
    seq_id: int,
    payload: bytes,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        transport = SerialTransport(config, state, service)
        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.is_open = True
        mock_serial.write = MagicMock(return_value=10)
        mock_serial.flush = MagicMock()
        transport.serial = mock_serial

        ok = await transport.send_raw(command_id, payload, seq_id=seq_id)
        assert ok is True
        assert mock_serial.write.called
        written_data: bytes = mock_serial.write.call_args[0][0]
        # HDLC frame starts and ends with FRAME_DELIMITER (0x7E)
        assert written_data.startswith(protocol.FRAME_DELIMITER)
        assert written_data.endswith(protocol.FRAME_DELIMITER)
        # HDLC decoding must succeed
        decoder = FrameDecoder()
        frames = list(decoder.process(written_data))
        assert len(frames) == 1
        assert frames[0].ok()
    finally:
        state.cleanup()


async def test_serial_transport_process_hdlc_log_frame(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_frame = MagicMock()
    mock_frame.address = protocol.HDLC_ADDRESS_LOG
    mock_frame.data = b"\x01\x02\x03\x04"
    mock_frame.raw_encoded = b"~test~"
    await transport.process_hdlc_frame(mock_frame)
    assert runtime_state.metrics.serial_frames_received.value >= 1
