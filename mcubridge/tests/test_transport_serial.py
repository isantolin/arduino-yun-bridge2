import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import serial

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pw_hdlc.decode import FrameDecoder
from pw_hdlc.encode import ui_frame
from pw_rpc import callback_client, packets as pw_packets
from pw_rpc.client import Client as PwRpcClient
from pw_rpc.descriptors import Channel as PwRpcChannel
from pw_rpc.internal.packet_pb2 import PacketType, RpcPacket
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.structures import PendingCommand

from mcubridge.security.security import LinkCipher, derive_link_keys
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


def _install_link_cipher(config: RuntimeConfig, state: RuntimeState) -> LinkCipher:
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    host_to_mcu, mcu_to_host = derive_link_keys(config.serial_shared_secret, nonce)
    state.link_cipher = LinkCipher(host_to_mcu, mcu_to_host)
    return LinkCipher(mcu_to_host, host_to_mcu)


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

        mcu_cipher = _install_link_cipher(config, state)
        encoded = ui_frame(
            protocol.HDLC_ADDRESS_CONSOLE,
            mcu_cipher.encrypt(protocol.HDLC_ADDRESS_CONSOLE, b"hi"),
        )
        transport = SerialTransport(config, state, service)
        await transport.feed_data(encoded)

        service.handle_mcu_frame.assert_awaited_once_with(
            Command.CMD_CONSOLE_WRITE.value, 0, pb.ConsoleWrite(data=b"hi")
        )
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

        mock_serial = MagicMock(spec=serial.Serial)
        target_baud = config.serial_safe_baud
        mock_serial.baudrate = config.serial_baud
        transport.serial = mock_serial
        mcu_cipher = _install_link_cipher(config, state)

        transport.state.connection_fsm.start_negotiating()
        fut = asyncio.get_running_loop().create_future()
        transport.negotiation_future = fut
        transport.negotiation_target_baudrate = target_baud

        cleartext = protocol.encode_hdlc_rpc(
            Command.CMD_SET_BAUDRATE_RESP.value,
            b"",
            call_id=0,
        )
        response_frame = next(iter(FrameDecoder().process(cleartext)))
        encoded = ui_frame(
            protocol.HDLC_ADDRESS_RPC,
            mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, response_frame.data),
        )
        await transport.feed_data(encoded)

        assert await fut
        assert mock_serial.baudrate == target_baud
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
        _install_link_cipher(config, state)
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
        _install_link_cipher(config, state)
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
        _install_link_cipher(config, state)
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
    assert transport.serial is mock_serial


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
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.write = MagicMock(return_value=10)
    mock_serial.flush = MagicMock()
    transport.serial = mock_serial

    send_task = asyncio.create_task(
        transport.send(Command.CMD_PIN_SUBSCRIBE.value, pb.PinSubscribeRequest(pin=7, enabled=True))
    )
    await asyncio.sleep(0.01)
    transport.correlate_frame(Status.ERROR.value, b"")
    assert await send_task is False


async def test_unary_rpc_calls_use_pigweed_client_and_correlate_concurrently(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    transport.serial = mock_serial

    outgoing: list[bytes] = []
    transport.rpc_client = PwRpcClient(
        cast(Any, callback_client).Impl(),
        [PwRpcChannel(protocol.HDLC_ADDRESS_RPC, outgoing.append)],
        protocol.PW_SERVICES,
    )

    first_request = pb.ClockSyncRequest(host_time_us=111)
    second_request = pb.ClockSyncRequest(host_time_us=222)
    first = asyncio.create_task(transport.send(Command.CMD_CLOCK_SYNC.value, first_request))
    second = asyncio.create_task(transport.send(Command.CMD_CLOCK_SYNC.value, second_request))

    for _ in range(100):
        if len(outgoing) == 2:
            break
        await asyncio.sleep(0)
    assert len(outgoing) == 2

    requests = [pw_packets.decode(packet) for packet in outgoing]
    assert requests[0].call_id != requests[1].call_id
    assert all(request.type == PacketType.REQUEST for request in requests)

    for request in reversed(requests):
        host_time_us = 111 if request.payload == first_request.SerializeToString() else 222
        response = pb.ClockSyncResponse(host_time_us=host_time_us, mcu_time_us=987)
        packet = RpcPacket(
            type=PacketType.RESPONSE,
            channel_id=request.channel_id,
            service_id=request.service_id,
            method_id=request.method_id,
            call_id=request.call_id,
            payload=response.SerializeToString(),
            status=0,
        )
        assert transport.rpc_client.process_packet(packet.SerializeToString()).ok()

    first_result, second_result = await asyncio.gather(first, second)
    assert isinstance(first_result, pb.ClockSyncResponse)
    assert isinstance(second_result, pb.ClockSyncResponse)
    assert first_result.host_time_us == 111
    assert second_result.host_time_us == 222


async def test_unary_rpc_decodes_empty_request_payload(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    transport.serial = mock_serial

    outgoing: list[bytes] = []
    transport.rpc_client = PwRpcClient(
        cast(Any, callback_client).Impl(),
        [PwRpcChannel(protocol.HDLC_ADDRESS_RPC, outgoing.append)],
        protocol.PW_SERVICES,
    )

    send_task = asyncio.create_task(transport.send(Command.CMD_GET_VERSION.value, b""))
    for _ in range(100):
        if outgoing:
            break
        await asyncio.sleep(0)
    assert len(outgoing) == 1

    request = pw_packets.decode(outgoing[0])
    assert request.type == PacketType.REQUEST
    assert request.payload == b""
    response = pb.VersionResponse(major=2, minor=8, patch=9)
    packet = RpcPacket(
        type=PacketType.RESPONSE,
        channel_id=request.channel_id,
        service_id=request.service_id,
        method_id=request.method_id,
        call_id=request.call_id,
        payload=response.SerializeToString(),
        status=0,
    )
    assert transport.rpc_client.process_packet(packet.SerializeToString()).ok()
    assert await send_task == response


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
    _install_link_cipher(runtime_config, runtime_state)
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
    _install_link_cipher(runtime_config, runtime_state)

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
    res_fail_status = await transport.send(Command.CMD_FILE_READ.value, pb.FileRead(path="/tmp/test"))
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
        _install_link_cipher(config, state)

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


async def test_connect_and_run_synchronizes_before_baudrate_negotiation(mocker: MockerFixture) -> None:
    config = _make_config()
    config.serial_baud = 230400
    config.serial_safe_baud = 115200
    state = create_runtime_state(config)
    service = AsyncMock()
    transport = SerialTransport(config, state, service)
    mock_serial = MagicMock(spec=serial.Serial)
    order: list[str] = []

    async def on_connected() -> None:
        order.append("synchronized")
        state.connection_fsm.connect()
        state.connection_fsm.synchronize()
        state.link_cipher = LinkCipher.for_host(
            config.serial_shared_secret,
            bytes(range(protocol.HANDSHAKE_NONCE_LENGTH)),
        )
        transport.stop_event.set()

    async def negotiate(target_baud: int) -> bool:
        assert target_baud == config.serial_baud
        assert state.is_synchronized
        assert state.link_cipher is not None
        order.append("baudrate")
        return True

    service.on_serial_connected.side_effect = on_connected
    transport.negotiate_baudrate = AsyncMock(side_effect=negotiate)
    transport.read_loop = AsyncMock()
    mocker.patch("serial.serial_for_url", return_value=mock_serial)
    mocker.patch.object(transport, "toggle_dtr", new=AsyncMock())

    try:
        await transport.connect_and_run()
        assert order == ["synchronized", "baudrate"]
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


# ---------------------------------------------------------------------------
# Pigweed RPC response mapping and dispatch (regressions for PR #58)
# ---------------------------------------------------------------------------

_PKT_REQUEST = 0
_PKT_RESPONSE = 1
_PKT_SERVER_ERROR = 5
_PW_STATUS_INTERNAL = 13


def _rpc_frame(
    command: Command,
    *,
    packet_type: int,
    call_id: int,
    payload: bytes | Any = b"",
    status: int = 0,
    cipher: LinkCipher | None = None,
) -> bytes:
    spec = protocol.get_operation(command.value)
    assert spec is not None
    assert spec.service_id is not None
    assert spec.method_id is not None
    packet = protocol.encode_rpc_packet(
        1,
        spec.service_id,
        spec.method_id,
        call_id,
        payload=payload,
        packet_type=cast(Any, packet_type),
        status=status,
    )
    if cipher is not None:
        packet = cipher.encrypt(protocol.HDLC_ADDRESS_RPC, packet)
    return ui_frame(protocol.HDLC_ADDRESS_RPC, packet)


def _service_with_mocked_dispatch(config: RuntimeConfig, state: RuntimeState) -> tuple[BridgeService, AsyncMock]:
    service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
    handle_mock = AsyncMock()
    service.handle_mcu_frame = handle_mock
    return service, handle_mock


async def test_link_sync_response_maps_to_resp_id_and_dispatches() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service, handle_mock = _service_with_mocked_dispatch(config, state)
        transport = SerialTransport(config, state, service)
        reply = pb.LinkSync(nonce=b"\x01" * 16, tag=b"\x02" * 16)

        await transport.feed_data(
            _rpc_frame(Command.CMD_LINK_SYNC, packet_type=_PKT_RESPONSE, call_id=7, payload=reply)
        )

        handle_mock.assert_awaited_once()
        assert handle_mock.await_args is not None
        cmd_id, seq_id, payload = handle_mock.await_args.args
        assert cmd_id == Command.CMD_LINK_SYNC_RESP.value
        assert seq_id == 7
        assert isinstance(payload, pb.LinkSync)
        assert payload.nonce == reply.nonce
    finally:
        state.cleanup()


async def test_unhandled_response_is_correlated_but_not_redispatched() -> None:
    """A DigitalWrite ack must resolve the pending command without a NOT_IMPLEMENTED loop."""
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service, handle_mock = _service_with_mocked_dispatch(config, state)
        transport = SerialTransport(config, state, service)
        mcu_cipher = _install_link_cipher(config, state)
        pending = PendingCommand(command_id=Command.CMD_DIGITAL_WRITE.value, expected_resp_ids=[])
        pending.sequence_id = 6
        transport.current_command = pending

        await transport.feed_data(
            _rpc_frame(Command.CMD_DIGITAL_WRITE, packet_type=_PKT_RESPONSE, call_id=6, cipher=mcu_cipher)
        )

        assert pending.success is True
        handle_mock.assert_not_awaited()
    finally:
        state.cleanup()


async def test_error_status_response_fails_pending_even_with_expected_response() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service, handle_mock = _service_with_mocked_dispatch(config, state)
        transport = SerialTransport(config, state, service)
        mcu_cipher = _install_link_cipher(config, state)
        pending = PendingCommand(
            command_id=Command.CMD_GET_VERSION.value,
            expected_resp_ids=[Command.CMD_GET_VERSION_RESP.value],
        )
        pending.sequence_id = 3
        transport.current_command = pending

        await transport.feed_data(
            _rpc_frame(
                Command.CMD_GET_VERSION,
                packet_type=_PKT_RESPONSE,
                call_id=3,
                payload=pb.VersionResponse(major=1),
                status=_PW_STATUS_INTERNAL,
                cipher=mcu_cipher,
            )
        )

        assert pending.success is False
        assert pending.failure_status == Status.ERROR.value
        handle_mock.assert_awaited_once()
        assert handle_mock.await_args is not None
        assert handle_mock.await_args.args[0] == Status.ERROR.value
    finally:
        state.cleanup()


async def test_server_error_packet_maps_to_status_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service, handle_mock = _service_with_mocked_dispatch(config, state)
        transport = SerialTransport(config, state, service)
        mcu_cipher = _install_link_cipher(config, state)

        await transport.feed_data(
            _rpc_frame(
                Command.CMD_DIGITAL_READ,
                packet_type=_PKT_SERVER_ERROR,
                call_id=9,
                cipher=mcu_cipher,
            )
        )

        handle_mock.assert_awaited_once()
        assert handle_mock.await_args is not None
        assert handle_mock.await_args.args[:2] == (Status.ERROR.value, 9)
    finally:
        state.cleanup()


async def test_mcu_request_is_dispatched_by_pigweed_service_and_method() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service, handle_mock = _service_with_mocked_dispatch(config, state)
        transport = SerialTransport(config, state, service)
        mcu_cipher = _install_link_cipher(config, state)

        await transport.feed_data(
            _rpc_frame(
                Command.CMD_DATASTORE_GET,
                packet_type=_PKT_REQUEST,
                call_id=4,
                cipher=mcu_cipher,
            )
        )

        handle_mock.assert_awaited_once()
        assert handle_mock.await_args is not None
        spec = protocol.get_operation(Command.CMD_DATASTORE_GET.value)
        assert spec is not None and spec.service_id is not None and spec.method_id is not None
        assert handle_mock.await_args.args[:2] == ((spec.service_id, spec.method_id), 4)
    finally:
        state.cleanup()


async def test_unparseable_response_payload_is_forwarded_as_bytes() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service, handle_mock = _service_with_mocked_dispatch(config, state)
        transport = SerialTransport(config, state, service)
        garbage = b"\x0a\xff"  # length-delimited field claiming 255 bytes: truncated

        await transport.feed_data(
            _rpc_frame(Command.CMD_LINK_SYNC, packet_type=_PKT_RESPONSE, call_id=2, payload=garbage)
        )

        handle_mock.assert_awaited_once()
        assert handle_mock.await_args is not None
        cmd_id, _seq, payload = handle_mock.await_args.args
        assert cmd_id == Command.CMD_LINK_SYNC_RESP.value
        assert payload == garbage
    finally:
        state.cleanup()


async def test_malformed_rpc_packet_counts_decode_error() -> None:
    from pw_hdlc.encode import ui_frame

    config = _make_config()
    state = create_runtime_state(config)
    try:
        service, handle_mock = _service_with_mocked_dispatch(config, state)
        transport = SerialTransport(config, state, service)
        mcu_cipher = _install_link_cipher(config, state)

        await transport.feed_data(
            ui_frame(
                protocol.HDLC_ADDRESS_RPC,
                mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, b"\x0a\xff"),
            )
        )

        assert state.serial_decode_errors == 1
        handle_mock.assert_not_awaited()
    finally:
        state.cleanup()


async def test_frames_without_service_or_unknown_address_are_consumed(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    from pw_hdlc.encode import ui_frame

    transport = SerialTransport(runtime_config, runtime_state, None)
    mcu_cipher = _install_link_cipher(runtime_config, runtime_state)
    before = runtime_state.metrics.serial_frames_received.value

    await transport.feed_data(
        ui_frame(
            protocol.HDLC_ADDRESS_CONSOLE,
            mcu_cipher.encrypt(protocol.HDLC_ADDRESS_CONSOLE, b"hi"),
        )
    )
    await transport.feed_data(
        _rpc_frame(Command.CMD_DATASTORE_GET, packet_type=_PKT_REQUEST, call_id=1, cipher=mcu_cipher)
    )
    await transport.feed_data(ui_frame(0x55, mcu_cipher.encrypt(0x55, b"ignored")))

    assert runtime_state.metrics.serial_frames_received.value == before + 3
    assert runtime_state.serial_decode_errors == 0


async def test_correlate_frame_debug_paths(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, mocker: MockerFixture
) -> None:
    import mcubridge.transport.serial as serial_mod

    mocker.patch.object(serial_mod.logger, "is_enabled_for", return_value=True)
    debug = mocker.patch.object(serial_mod.logger, "debug")
    transport = SerialTransport(runtime_config, runtime_state, None)

    stale = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    stale.sequence_id = 10
    transport.current_command = stale
    transport.correlate_frame(Command.CMD_GET_VERSION_RESP.value, b"", seq_id=11)
    assert stale.success is None

    stale.mark_success(b"done")
    transport.correlate_frame(Command.CMD_GET_VERSION_RESP.value, b"late", seq_id=10)
    assert stale.response_payload == b"done"

    events = [c.args[0] for c in debug.call_args_list]
    assert "ignoring_stale_frame_sequence_mismatch" in events
    assert "Pending command already resolved, ignoring further correlation" in events


async def test_send_raw_console_payload_variants(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mcu_cipher = _install_link_cipher(runtime_config, runtime_state)
    mock_serial = MagicMock(spec=serial.Serial)
    transport.serial = mock_serial

    assert await transport.send_raw(Command.CMD_CONSOLE_WRITE.value, pb.ConsoleWrite(data=b"abc"))
    assert await transport.send_raw(Command.CMD_CONSOLE_WRITE.value, cast(Any, object()))

    written = [next(iter(FrameDecoder().process(c.args[0]))) for c in mock_serial.write.call_args_list]
    assert written[0].address == protocol.HDLC_ADDRESS_CONSOLE
    assert mcu_cipher.decrypt(written[0].address, written[0].data) == b"abc"
    assert written[1].address == protocol.HDLC_ADDRESS_CONSOLE
    assert mcu_cipher.decrypt(written[1].address, written[1].data) == b""


async def test_bidirectional_ack_response_returns_true_and_is_not_redispatched() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service, handle_mock = _service_with_mocked_dispatch(config, state)
        transport = SerialTransport(config, state, service)
        mcu_cipher = _install_link_cipher(config, state)
        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.is_open = True
        transport.serial = mock_serial

        outgoing: list[bytes] = []

        def queue_rpc_packet(packet: bytes) -> None:
            outgoing.append(packet)
            transport.queue_rpc_packet(packet)

        transport.rpc_client = PwRpcClient(
            cast(Any, callback_client).Impl(),
            [PwRpcChannel(protocol.HDLC_ADDRESS_RPC, queue_rpc_packet)],
            protocol.PW_SERVICES,
        )
        send_task = asyncio.create_task(transport.send(Command.CMD_MAILBOX_PUSH.value, pb.MailboxPush(data=b"PING")))
        for _ in range(100):
            if outgoing:
                break
            await asyncio.sleep(0)
        assert len(outgoing) == 1

        request = pw_packets.decode(outgoing[0])
        assert request.type == PacketType.REQUEST
        response = RpcPacket(
            type=PacketType.RESPONSE,
            channel_id=request.channel_id,
            service_id=request.service_id,
            method_id=request.method_id,
            call_id=request.call_id,
            payload=b"",
            status=0,
        )
        response_frame = ui_frame(
            protocol.HDLC_ADDRESS_RPC,
            mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, response.SerializeToString()),
        )
        await transport.feed_data(response_frame)

        assert await send_task == pb.Empty()
        handle_mock.assert_not_awaited()
    finally:
        state.cleanup()


def _make_rpc_packet(packet_type: int, call_id: int = 41) -> bytes:
    spec = protocol.get_operation(Command.CMD_CLOCK_SYNC.value)
    assert spec is not None and spec.service_id is not None and spec.method_id is not None
    return RpcPacket(
        type=cast(Any, packet_type),
        channel_id=protocol.HDLC_ADDRESS_RPC,
        service_id=spec.service_id,
        method_id=spec.method_id,
        call_id=call_id,
    ).SerializeToString()


async def test_queue_rpc_packet_tracks_request_and_client_error_lifecycle(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    runtime_config.serial_shared_secret = b""
    runtime_state.serial_tx_allowed.set()
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.write = MagicMock(return_value=16)
    mock_serial.flush = MagicMock()
    transport.serial = mock_serial
    request = _make_rpc_packet(PacketType.REQUEST)
    request_key = (
        protocol.HDLC_ADDRESS_RPC,
        pw_packets.decode(request).service_id,
        pw_packets.decode(request).method_id,
        pw_packets.decode(request).call_id,
    )

    transport.queue_rpc_packet(request)
    assert request_key in cast(set[tuple[int, int, int, int]], transport.__dict__["_active_rpc_packet_ids"])

    client_error = _make_rpc_packet(PacketType.CLIENT_ERROR)
    transport.queue_rpc_packet(client_error)
    assert request_key not in cast(set[tuple[int, int, int, int]], transport.__dict__["_active_rpc_packet_ids"])

    # A response packet is written but must not be registered as a new active call.
    response = _make_rpc_packet(PacketType.RESPONSE)
    transport.queue_rpc_packet(response)
    assert request_key not in transport.__dict__["_active_rpc_packet_ids"]

    for _ in range(100):
        if mock_serial.write.call_count == 3:
            break
        await asyncio.sleep(0.01)
    assert mock_serial.write.call_count == 3


def test_queue_rpc_packet_ignores_malformed_packet(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.queue_rpc_packet(b"\xff")
    assert transport.__dict__["_active_rpc_packet_ids"] == set()


def test_queue_rpc_packet_rejects_calls_outside_event_loop(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    packet = _make_rpc_packet(int(PacketType.REQUEST))
    decoded = pw_packets.decode(packet)
    key = (decoded.channel_id, decoded.service_id, decoded.method_id, decoded.call_id)

    with pytest.raises(RuntimeError, match="outside the asyncio event loop"):
        transport.queue_rpc_packet(packet)

    assert key in transport.__dict__["_active_rpc_packet_ids"]


async def test_rpc_write_refuses_unauthenticated_link_after_flow_control_timeout(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: Any
) -> None:
    import mcubridge.transport.serial as serial_mod

    monkeypatch.setattr(serial_mod, "FLOW_CONTROL_WAIT_TIMEOUT_SECONDS", 0.0)
    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.queue_rpc_packet(_make_rpc_packet(int(PacketType.REQUEST)))
    await asyncio.sleep(0.01)

    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.write = MagicMock(side_effect=OSError("serial write failed"))
    mock_serial.flush = MagicMock()
    transport.serial = mock_serial
    runtime_state.serial_tx_allowed.clear()
    runtime_state.mcu_is_paused = True

    transport.queue_rpc_packet(_make_rpc_packet(int(PacketType.REQUEST)))
    await asyncio.sleep(0.05)

    assert runtime_state.serial_tx_allowed.is_set()
    assert runtime_state.mcu_is_paused is False
    mock_serial.write.assert_not_called()

    runtime_config.serial_shared_secret = b""
    transport.queue_rpc_packet(_make_rpc_packet(int(PacketType.REQUEST)))
    await asyncio.sleep(0.05)
    mock_serial.write.assert_called_once()


def _private_attribute(instance: object, name: str) -> Any:
    return getattr(instance, name)


def test_decode_rpc_request_rejects_invalid_payloads(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    from types import SimpleNamespace

    transport = SerialTransport(runtime_config, runtime_state, None)
    spec = protocol.get_operation(Command.CMD_CLOCK_SYNC.value)
    assert spec is not None

    decode_rpc_request = cast(
        Callable[[Any, Any], Any],
        _private_attribute(transport, "_decode_rpc_request"),
    )
    assert decode_rpc_request(spec, cast(Any, object())) is None
    assert decode_rpc_request(spec, b"\x0a\xff") is None
    assert decode_rpc_request(SimpleNamespace(request_type=None, name="missing"), b"") is None


def test_get_rpc_method_returns_none_when_descriptor_is_missing(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    rpc_client = MagicMock()
    rpc_client.channel.return_value.method.side_effect = KeyError("missing method")
    transport.rpc_client = rpc_client

    spec = protocol.get_operation(Command.CMD_CLOCK_SYNC.value)
    assert spec is not None
    get_rpc_method = cast(Callable[[Any], Any], _private_attribute(transport, "_get_rpc_method"))
    assert get_rpc_method(spec) is None


async def test_invoke_rpc_completion_without_response_and_error_callback(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: Any
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    rpc_method = MagicMock()
    call = MagicMock()
    cancel_errors: list[Exception | None] = [
        RuntimeError("cancel race"),
        None,
        ValueError("cancel on task abort"),
    ]

    def cancel_with_optional_error() -> None:
        err = cancel_errors.pop(0)
        if err is not None:
            raise err

    call.cancel.side_effect = cancel_with_optional_error

    def complete_without_payload(
        _request: Any, *, on_next: Any, on_completed: Any, on_error: Any
    ) -> Any:
        on_completed(call, MagicMock(ok=MagicMock(return_value=True)))
        on_completed(call, MagicMock(ok=MagicMock(return_value=True)))
        return call

    rpc_method.invoke.side_effect = complete_without_payload
    invoke_rpc_with_retries = cast(
        Callable[[Any, Any, str, float], Awaitable[Any]],
        _private_attribute(transport, "_invoke_rpc_with_retries"),
    )
    active_rpc_calls = _private_attribute(transport, "_active_rpc_calls")
    result = await invoke_rpc_with_retries(
        rpc_method, pb.ClockSyncRequest(host_time_us=1), "ClockSync/Sync", 1.0
    )
    assert result
    assert call not in active_rpc_calls

    def fail_callback(
        _request: Any, *, on_next: Any, on_completed: Any, on_error: Any
    ) -> Any:
        active_rpc_calls.clear()
        on_error(call, RuntimeError("remote failure"))
        on_error(call, RuntimeError("duplicate failure"))
        return call

    rpc_method.invoke.side_effect = fail_callback
    result = await invoke_rpc_with_retries(
        rpc_method, pb.ClockSyncRequest(host_time_us=1), "ClockSync/Sync", 1.0
    )
    assert not result
    assert call not in active_rpc_calls

    def complete_with_non_ok_status(
        _request: Any, *, on_next: Any, on_completed: Any, on_error: Any
    ) -> Any:
        on_completed(call, MagicMock(ok=MagicMock(return_value=False)))
        return call

    rpc_method.invoke.side_effect = complete_with_non_ok_status
    result = await invoke_rpc_with_retries(
        rpc_method, pb.ClockSyncRequest(host_time_us=1), "ClockSync/Sync", 1.0
    )
    assert not result
    assert call not in active_rpc_calls

    def raise_invoke_error(
        _request: Any, *, on_next: Any, on_completed: Any, on_error: Any
    ) -> Any:
        raise RuntimeError("invoke error")

    rpc_method.invoke.side_effect = raise_invoke_error
    result = await invoke_rpc_with_retries(
        rpc_method, pb.ClockSyncRequest(host_time_us=1), "ClockSync/Sync", 1.0
    )
    assert not result

    def hang_without_completion(
        _request: Any, *, on_next: Any, on_completed: Any, on_error: Any
    ) -> Any:
        return call

    transport.max_attempts = 2
    rpc_method.invoke.side_effect = hang_without_completion
    monkeypatch.setattr("mcubridge.transport.serial.SERIAL_HANDSHAKE_BACKOFF_BASE", 0.0)
    result = await invoke_rpc_with_retries(
        rpc_method, pb.ClockSyncRequest(host_time_us=1), "ClockSync/Sync", 0.001
    )
    assert not result
    assert call.cancel.call_count == 2

    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    transport.serial = mock_serial
    assert not await transport.send(Command.CMD_CLOCK_SYNC.value, cast(Any, object()))

    rpc_client = MagicMock()
    rpc_client.channel.return_value.method.return_value = rpc_method
    transport.rpc_client = rpc_client
    cancel_task = asyncio.create_task(
        transport.send(Command.CMD_CLOCK_SYNC.value, pb.ClockSyncRequest(host_time_us=1), timeout=10.0)
    )
    await asyncio.sleep(0)
    cancel_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancel_task
    assert call.cancel.call_count == 3

    rpc_client.channel.return_value.method.side_effect = KeyError("missing method")
    assert not await transport.send(Command.CMD_CLOCK_SYNC.value, pb.ClockSyncRequest(host_time_us=1))

    pending_write = asyncio.create_task(asyncio.sleep(10))
    _private_attribute(transport, "_rpc_write_tasks").add(pending_write)
    ok_call = MagicMock()
    failing_call = MagicMock()
    failing_call.cancel.side_effect = RuntimeError("cancel failed")
    active_rpc_calls.extend([ok_call, failing_call])
    await transport.reset()
    ok_call.cancel.assert_called_once()
    failing_call.cancel.assert_called_once()
