import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from dirty_equals import IsInstance
import pytest
from pytest_mock import MockerFixture
import serial
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
from mcubridge.security.security import LinkCipher
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import (
    SerialTransport,
    resolve_serial_url,
)


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=57600,
        serial_shared_secret=b"secret_1234567890",
        serial_response_timeout=0.2,
        serial_retry_timeout=0.1,
        serial_retry_attempts=2,
        allowed_commands=("echo",),
        file_system_root="/tmp/mcubridge-test-serial",
    )


def _install_link_cipher(config: RuntimeConfig, state: RuntimeState) -> LinkCipher:
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    state.link_cipher = LinkCipher.for_host(config.serial_shared_secret, nonce)
    return LinkCipher.for_mcu(config.serial_shared_secret, nonce)


@pytest.mark.parametrize(
    ("raw_port", "expected_url"),
    [
        ("", ""),
        ("/dev/ttyATH0", "/dev/ttyATH0"),
        ("  /dev/ttyUSB0  ", "/dev/ttyUSB0"),
        ("./virtual_pty", "./virtual_pty"),
        ("wifi://192.168.1.50", "socket://192.168.1.50:9000"),
        ("wifi://192.168.1.50:2323", "socket://192.168.1.50:2323"),
        ("tcp://10.0.0.10", "socket://10.0.0.10:9000"),
        ("tcp://10.0.0.10:8888", "socket://10.0.0.10:8888"),
        ("socket://127.0.0.1", "socket://127.0.0.1:9000"),
        ("socket://127.0.0.1:5555", "socket://127.0.0.1:5555"),
        ("192.168.4.1:9000", "socket://192.168.4.1:9000"),
        ("rfc2217://localhost:7000", "rfc2217://localhost:7000"),
    ],
)
def test_resolve_serial_url(raw_port: str, expected_url: str) -> None:
    assert resolve_serial_url(raw_port) == expected_url


async def test_serial_transport_feed_and_process() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        service.handle_mcu_frame = AsyncMock()
        transport = SerialTransport(config, state, service)
        mcu_cipher = _install_link_cipher(config, state)

        # Feed valid HDLC console frame
        raw_frame = ui_frame(
            protocol.HDLC_ADDRESS_CONSOLE,
            mcu_cipher.encrypt(protocol.HDLC_ADDRESS_CONSOLE, b"hello"),
        )
        await transport.feed_data(raw_frame)
        service.handle_mcu_frame.assert_awaited_once_with(
            Command.CMD_CONSOLE_WRITE.value,
            0,
            pb.ConsoleWrite(data=b"hello"),
        )

        # Feed corrupted frame (CRC error)
        corrupted = bytearray(raw_frame)
        corrupted[-2] ^= 0xFF
        transport.negotiate_baudrate = AsyncMock(return_value=True)
        for _ in range(config.serial_fallback_threshold):
            await transport.feed_data(bytes(corrupted))

        assert state.serial_decode_errors >= config.serial_fallback_threshold
        transport.negotiate_baudrate.assert_awaited_once_with(config.serial_safe_baud)
    finally:
        state.cleanup()


async def test_serial_transport_send_and_correlate() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        transport = SerialTransport(config, state, service)
        mcu_cipher = _install_link_cipher(config, state)
        state.serial_tx_allowed.set()

        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.is_open = True
        written: list[bytes] = []

        def _write(data: bytes) -> int:
            written.append(data)
            frames = list(FrameDecoder().process(data))
            if frames and frames[0].ok():
                frame = frames[0]
                if frame.address == protocol.HDLC_ADDRESS_RPC:
                    pkt = pw_packets.decode(mcu_cipher.decrypt(frame.address, bytes(frame.data)))
                    resp_pkt = RpcPacket(
                        type=PacketType.RESPONSE,
                        channel_id=pkt.channel_id,
                        service_id=pkt.service_id,
                        method_id=pkt.method_id,
                        call_id=pkt.call_id,
                        payload=pb.VersionResponse(major=2, minor=1, patch=0).SerializeToString(),
                        status=0,
                    )
                    resp_frame = ui_frame(
                        protocol.HDLC_ADDRESS_RPC,
                        mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, resp_pkt.SerializeToString()),
                    )
                    asyncio.get_running_loop().call_soon_threadsafe(
                        asyncio.create_task,
                        transport.feed_data(resp_frame),
                    )
            return len(data)

        mock_serial.write.side_effect = _write
        transport.serial = mock_serial

        resp = await transport.send(Command.CMD_GET_VERSION.value, b"")
        assert resp == IsInstance(pb.VersionResponse)
        assert isinstance(resp, pb.VersionResponse)
        assert resp.major == 2
        assert len(written) == 1
    finally:
        state.cleanup()


async def test_serial_transport_send_timeout_and_reset() -> None:
    config = _make_config()
    config.serial_response_timeout = 0.05
    config.serial_retry_timeout = 0.05
    config.serial_retry_attempts = 1
    state = create_runtime_state(config)
    try:
        transport = SerialTransport(config, state, None)
        _install_link_cipher(config, state)
        state.serial_tx_allowed.set()

        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.is_open = True
        mock_serial.write.return_value = 10
        transport.serial = mock_serial

        res = await transport.send(Command.CMD_GET_VERSION.value, b"", timeout=0.05)
        assert not res

        await transport.reset()
        await transport.stop()
        mock_serial.close.assert_called_once()
    finally:
        state.cleanup()


@given(
    cmd_id=st.sampled_from([Command.CMD_CONSOLE_WRITE.value, Command.CMD_DIGITAL_WRITE.value]),
    payload=st.binary(min_size=0, max_size=64),
)
def test_serial_transport_hdlc_roundtrip_property(cmd_id: int, payload: bytes) -> None:
    if cmd_id == Command.CMD_CONSOLE_WRITE.value:
        encoded = protocol.encode_hdlc_console(payload)
    else:
        encoded = protocol.encode_hdlc_rpc(cmd_id, payload)
    frames = list(FrameDecoder().process(encoded))
    assert len(frames) == 1
    assert frames[0].ok()


async def test_serial_transport_ToggleDtr_and_Baudrate(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    _install_link_cipher(runtime_config, runtime_state)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.dtr = True
    transport.serial = mock_serial

    await transport.toggle_dtr()
    assert mock_serial.dtr

    transport.switch_local_baudrate(230400)
    assert mock_serial.baudrate == 230400

    # Test negotiate_baudrate timeout failure
    runtime_state.serial_tx_allowed.set()
    mock_serial.write.return_value = 10
    neg_ok = await transport.negotiate_baudrate(230400)
    assert not neg_ok


async def test_serial_transport_read_loop_and_stop(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mcu_cipher = _install_link_cipher(runtime_config, runtime_state)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True

    frame_bytes = ui_frame(
        protocol.HDLC_ADDRESS_CONSOLE,
        mcu_cipher.encrypt(protocol.HDLC_ADDRESS_CONSOLE, b"ping"),
    )
    calls = 0

    def _read_until(*_args: Any, **_kwargs: Any) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return frame_bytes
        transport.stop_event.set()
        return b""

    mock_serial.read_until.side_effect = _read_until
    await transport.read_loop(mock_serial)
    assert runtime_state.metrics.serial_frames_received.value >= 1


async def test_serial_transport_flow_control_auto_resume(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcubridge.transport.serial as serial_mod

    monkeypatch.setattr(serial_mod, "FLOW_CONTROL_WAIT_TIMEOUT_SECONDS", 0.02)
    transport = SerialTransport(runtime_config, runtime_state, None)
    _install_link_cipher(runtime_config, runtime_state)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    mock_serial.write.return_value = 5
    transport.serial = mock_serial

    runtime_state.serial_tx_allowed.clear()
    runtime_state.mcu_is_paused = True

    sent = await transport.send_raw(Command.CMD_CONSOLE_WRITE.value, b"hi")
    assert sent
    assert runtime_state.serial_tx_allowed.is_set()


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
        transport.send(Command.CMD_FILE_READ.value, pb.FileRead(path="/tmp/test"))
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
    ok_call = MagicMock()
    failing_call = MagicMock(cancel=MagicMock(side_effect=RuntimeError("cancel failed")))
    cast(list[Any], transport.__dict__["_active_rpc_calls"]).extend([ok_call, failing_call])
    await transport.reset()
    ok_call.cancel.assert_called_once()
    failing_call.cancel.assert_called_once()

    toggle_dtr: Callable[[], Awaitable[None]] = transport.toggle_dtr
    await toggle_dtr()

    stop_fn: Callable[[], Awaitable[None]] = transport.stop
    await stop_fn()
    assert transport.stop_event.is_set()

    send_fn: Callable[[int, bytes], Awaitable[bool | bytes | Any]] = transport.send
    assert not await send_fn(Command.CMD_GET_VERSION.value, b"")

    send_raw_fn: Callable[[int, bytes], Awaitable[bool]] = transport.send_raw
    assert not await send_raw_fn(Command.CMD_GET_VERSION.value, b"")


async def test_serial_transport_correlate_edge_cases(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    from mcubridge.protocol.protocol import Status
    from mcubridge.protocol.structures import PendingCommand

    transport = SerialTransport(runtime_config, runtime_state, None)
    correlate_fn: Callable[[int, bytes, int | None], None] = transport.correlate_frame

    # 1. No current command
    transport.current_command = None
    correlate_fn(Status.OK.value, b"", 1)

    # 2. Sequence mismatch
    cmd = PendingCommand(command_id=Command.CMD_GET_VERSION.value, sequence_id=10)
    transport.current_command = cmd
    correlate_fn(Command.CMD_GET_VERSION_RESP.value, b"", 99)
    assert not cmd.completion.is_set()

    # 3. Direct command_id match or paired_request_id match
    curr1 = PendingCommand(command_id=Command.CMD_GET_VERSION.value, sequence_id=10)
    transport.current_command = curr1
    correlate_fn(Command.CMD_GET_VERSION_RESP.value, b"v1", 10)
    assert curr1.completion.is_set()
    assert curr1.success is True

    # 4. Already completed pending command
    correlate_fn(Command.CMD_GET_VERSION_RESP.value, b"v2", 10)
    assert curr1.response_payload == b"v1"

    # 5. Failure status code
    curr2 = PendingCommand(command_id=Command.CMD_GET_VERSION.value, sequence_id=11)
    transport.current_command = curr2
    correlate_fn(Status.MALFORMED.value, b"", 11)
    assert curr2.completion.is_set()
    assert curr2.success is False

    # 6. Success status code when expected_resp_ids is empty
    curr3 = PendingCommand(command_id=Command.CMD_SET_PIN_MODE.value, expected_resp_ids=[], sequence_id=12)
    transport.current_command = curr3
    correlate_fn(Status.OK.value, b"", 12)
    assert curr3.completion.is_set()
    assert curr3.success is True


async def test_serial_transport_error_handlers_and_lifecycle(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock()
    service.on_serial_connected = AsyncMock()
    service.on_serial_disconnected = AsyncMock(side_effect=RuntimeError("disconnect hook fail"))
    service.handle_mcu_frame = AsyncMock()

    transport = SerialTransport(runtime_config, runtime_state, service)
    _install_link_cipher(runtime_config, runtime_state)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True

    # 1. switch_local_baudrate exception
    type(mock_serial).baudrate = PropertyMock(side_effect=OSError("baud fail"))
    transport.serial = mock_serial
    with pytest.raises(RuntimeError, match="UART access failed"):
        transport.switch_local_baudrate(115200)
    type(mock_serial).baudrate = PropertyMock(return_value=115200)

    # 2. toggle_dtr exception
    type(mock_serial).dtr = PropertyMock(side_effect=OSError("dtr fail"))
    await transport.toggle_dtr()
    type(mock_serial).dtr = PropertyMock(return_value=True)

    # 3. stop() with cancel_read / close exceptions
    mock_serial.cancel_read.side_effect = OSError("cancel fail")
    mock_serial.close.side_effect = OSError("close fail")
    await transport.stop()
    transport.stop_event.clear()

    # 4. read_loop exception when stop_event is NOT set vs set
    mock_serial.read_until.side_effect = OSError("read error")
    await transport.read_loop(mock_serial)

    transport.stop_event.set()
    await transport.read_loop(mock_serial)
    transport.stop_event.clear()

    # 5. read_loop normal exit when stop_event is set during read_until
    def _readuntil_during_stop(*_args: Any, **_kwargs: Any) -> Any:
        transport.stop_event.set()
        raise OSError("closed")

    mock_serial.read_until.side_effect = _readuntil_during_stop
    await transport.read_loop(mock_serial)
    transport.stop_event.clear()

    # 6. connect_and_run wireless loss & serial loss & baudrate negotiation failure
    monkeypatch.setattr(serial, "serial_for_url", lambda *_a, **_kw: mock_serial)
    transport.toggle_dtr = AsyncMock()

    async def _fast_read_loop(_ser: Any) -> None:
        await asyncio.sleep(0.01)

    transport.read_loop = _fast_read_loop

    # Baudrate negotiation failure
    runtime_config.serial_baud = 230400
    runtime_config.serial_safe_baud = 115200
    transport.negotiate_baudrate = AsyncMock(return_value=False)
    with pytest.raises(ConnectionError, match="Baudrate negotiation failed"):
        await transport.connect_and_run()

    # Serial connection lost (stop_event not set)
    transport.negotiate_baudrate = AsyncMock(return_value=True)
    with pytest.raises(ConnectionError, match="Serial connection lost"):
        await transport.connect_and_run()

    # Wireless connection lost
    runtime_config.serial_port = "tcp://127.0.0.1:9000"
    with pytest.raises(ConnectionError, match="Wireless network connection lost"):
        await transport.connect_and_run()

    # 7. run() with CancelledError and SerialHandshakeFatal
    transport.connect_and_run = AsyncMock(side_effect=asyncio.CancelledError())
    await transport.run()

    transport.connect_and_run = AsyncMock(side_effect=SerialHandshakeFatal("fatal auth"))
    with pytest.raises(SerialHandshakeFatal):
        await transport.run()


async def test_serial_transport_process_hdlc_frame_branches(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcubridge.transport.serial as serial_mod
    from mcubridge.protocol.structures import PendingCommand

    service = MagicMock()
    service.handle_mcu_frame = AsyncMock()
    transport = SerialTransport(runtime_config, runtime_state, service)
    mcu_cipher = _install_link_cipher(runtime_config, runtime_state)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    transport.serial = mock_serial

    monkeypatch.setattr(serial_mod.logger, "is_enabled_for", lambda _lvl: True)

    # 1. Baudrate negotiation frame interception
    runtime_state.connection_fsm.start_negotiating()
    loop = asyncio.get_running_loop()
    transport.negotiation_future = loop.create_future()
    transport.negotiation_target_baudrate = 230400

    baud_pkt = protocol.encode_rpc_packet(
        Command.CMD_SET_BAUDRATE_RESP.value,
        pb.SetBaudratePacket(baudrate=230400),
        call_id=1,
    )
    baud_frame = ui_frame(
        protocol.HDLC_ADDRESS_RPC,
        mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, baud_pkt),
    )
    await transport.feed_data(baud_frame)
    assert transport.negotiation_future.done()
    runtime_state.connection_fsm.connect()

    # 2. Uninitialized Protobuf message rejection in process_hdlc_frame
    fake_uninit = MagicMock(spec=pb.VersionResponse)
    fake_uninit.IsInitialized.return_value = False
    spec = protocol.get_operation(Command.CMD_GET_VERSION_RESP.value)
    if spec is not None:
        monkeypatch.setattr(spec, "response_type", lambda: fake_uninit)
        ver_pkt = protocol.encode_rpc_packet(Command.CMD_GET_VERSION_RESP.value, b"\x08\x01", call_id=2)
        ver_frame = ui_frame(
            protocol.HDLC_ADDRESS_RPC,
            mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, ver_pkt),
        )
        await transport.feed_data(ver_frame)
        assert runtime_state.serial_decode_errors >= 1

    # 3. reset() with active current_command
    pending = PendingCommand(command_id=Command.CMD_GET_VERSION.value)
    transport.current_command = pending
    await transport.reset()
    assert pending.success is False

    # 4. send() when send_raw returns False -> _FatalSerialError
    mock_serial2 = MagicMock(spec=serial.Serial)
    mock_serial2.is_open = True
    mock_serial2.write.side_effect = OSError("write error")
    transport.serial = mock_serial2
    runtime_state.serial_tx_allowed.set()
    res = await transport.send(Command.CMD_GET_VERSION.value, b"")
    assert res is False

    # 5. send() untrackable command (e.g. CMD_LINK_SYNC)
    mock_serial2.write.side_effect = None
    mock_serial2.write.return_value = 10
    runtime_state.link_cipher = None
    res_raw = await transport.send(
        Command.CMD_LINK_SYNC.value,
        pb.LinkSync(
            nonce=b"1234567890123456",
            tag=b"1234567890123456",
        ),
    )
    assert res_raw is True
    _install_link_cipher(runtime_config, runtime_state)

    # 6. negotiate_baudrate when send_raw fails vs succeeds
    mock_serial2.write.side_effect = OSError("baud write fail")
    res_neg_fail = await transport.negotiate_baudrate(230400)
    assert res_neg_fail is False

    def _complete_neg(_data: bytes) -> int:
        fut = transport.negotiation_future
        if fut and not fut.done():
            loop.call_soon_threadsafe(fut.set_result, True)
        return 10

    mock_serial2.write.side_effect = _complete_neg
    runtime_state.connection_fsm.synchronize()
    res_neg_ok = await transport.negotiate_baudrate(230400)
    assert res_neg_ok is True

    # 7. send() where completion is set with failure_status=None and success=False -> _RetryableSerialError
    transport.max_attempts = 1

    def _resolve_pending_without_status(_data: bytes) -> int:
        curr = transport.current_command
        if curr is not None:
            curr.success = False
            curr.failure_status = None
            loop.call_soon_threadsafe(curr.completion.set)
        return len(_data)

    mock_serial2.write = MagicMock(side_effect=_resolve_pending_without_status)
    res_fail_status = await transport.send(Command.CMD_FILE_READ.value, pb.FileRead(path="/tmp/test"))
    assert res_fail_status is False


def test_linux_serial_monkeypatch_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify pyserial scheme registration on import."""
    import sys

    assert "serial.urlhandler.protocol_wifi" in sys.modules
    assert "serial.urlhandler.protocol_tcp" in sys.modules


async def test_connect_and_run_taskgroup_multiple_exceptions(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    monkeypatch.setattr(serial, "serial_for_url", lambda *_a, **_kw: mock_serial)
    transport.toggle_dtr = AsyncMock()

    async def _failing_read_loop(_ser: Any) -> None:
        raise ValueError("read loop boom")

    transport.read_loop = _failing_read_loop
    service = MagicMock()
    service.on_serial_connected = AsyncMock(side_effect=RuntimeError("connect hook boom"))
    service.on_serial_disconnected = AsyncMock()
    transport.service = service

    with pytest.raises((ValueError, RuntimeError, BaseExceptionGroup)):
        await transport.connect_and_run()


async def test_check_baudrate_fallback_already_at_safe_baud(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    runtime_config.serial_baud = runtime_config.serial_safe_baud
    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.negotiate_baudrate = AsyncMock()
    transport.consecutive_crc_errors = runtime_config.serial_fallback_threshold - 1
    await transport.check_baudrate_fallback()
    transport.negotiate_baudrate.assert_not_called()


async def test_send_raw_disappeared_serial_inside_lock(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    transport.serial = mock_serial
    runtime_state.serial_tx_allowed.set()

    # Acquire lock first, then clear transport.serial before send_raw acquires it
    async with transport._serial_write_lock:
        task = asyncio.create_task(transport.send_raw(Command.CMD_GET_VERSION.value, b""))
        await asyncio.sleep(0.01)
        transport.serial = None

    assert not await task


async def test_send_raw_console_bytes_vs_protobuf() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        transport = SerialTransport(config, state, None)
        mcu_cipher = _install_link_cipher(config, state)
        state.serial_tx_allowed.set()

        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.is_open = True
        written: list[bytes] = []
        mock_serial.write.side_effect = lambda d: written.append(d) or len(d)
        transport.serial = mock_serial

        # Send console write with Protobuf message
        ok = await transport.send_raw(Command.CMD_CONSOLE_WRITE.value, pb.ConsoleWrite(data=b"hello"))
        assert ok is True
        assert len(written) == 1
        frames = list(FrameDecoder().process(written[0]))
        assert len(frames) == 1
        assert frames[0].ok()
        assert frames[0].address == protocol.HDLC_ADDRESS_CONSOLE
        assert mcu_cipher.decrypt(frames[0].address, bytes(frames[0].data)) == b"hello"

        # Send console write with unexpected payload type
        written.clear()
        await transport.send_raw(Command.CMD_CONSOLE_WRITE.value, 12345)  # type: ignore[arg-type]
        assert len(written) == 1
        frames = list(FrameDecoder().process(written[0]))
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
        for _ in range(100):
            if not transport.__dict__["_rpc_write_tasks"]:
                break
            await asyncio.sleep(0.01)
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
        if mock_serial.write.call_count == 3 and not transport.__dict__["_rpc_write_tasks"]:
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
    transport.serial = None
    transport.queue_rpc_packet(_make_rpc_packet(int(PacketType.REQUEST)))
    for _ in range(100):
        if not transport.__dict__["_rpc_write_tasks"]:
            break
        await asyncio.sleep(0.01)

    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    transport.serial = mock_serial
    runtime_state.serial_tx_allowed.clear()
    runtime_state.mcu_is_paused = True

    transport.queue_rpc_packet(_make_rpc_packet(int(PacketType.REQUEST)))
    for _ in range(100):
        if not transport.__dict__["_rpc_write_tasks"]:
            break
        await asyncio.sleep(0.01)

    assert runtime_state.serial_tx_allowed.is_set()
    assert runtime_state.mcu_is_paused is False
    mock_serial.write.assert_not_called()

    runtime_config.serial_shared_secret = b""
    mock_serial.write = MagicMock(side_effect=OSError("serial write failed"))
    transport.queue_rpc_packet(_make_rpc_packet(int(PacketType.REQUEST)))
    for _ in range(100):
        if not transport.__dict__["_rpc_write_tasks"]:
            break
        await asyncio.sleep(0.01)
    mock_serial.write.assert_called_once()


def _private_attribute(instance: object, name: str) -> Any:
    return getattr(instance, name)


async def test_decode_rpc_request_rejects_invalid_payloads(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    from types import SimpleNamespace

    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    transport.serial = mock_serial
    spec = protocol.get_operation(Command.CMD_CLOCK_SYNC.value)
    assert spec is not None

    decode_rpc_request = cast(
        Callable[[Any, Any], Any],
        _private_attribute(transport, "_decode_rpc_request"),
    )
    assert decode_rpc_request(spec, cast(Any, object())) is None
    assert decode_rpc_request(spec, b"\x0a\xff") is None
    assert decode_rpc_request(SimpleNamespace(request_type=None, name="missing"), b"") is None
    assert not await transport.send(Command.CMD_CLOCK_SYNC.value, cast(Any, object()))


async def test_get_rpc_method_returns_none_when_descriptor_is_missing(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    mock_serial = MagicMock(spec=serial.Serial)
    mock_serial.is_open = True
    transport.serial = mock_serial
    rpc_client = MagicMock()
    rpc_client.channel.return_value.method.side_effect = KeyError("missing method")
    transport.rpc_client = rpc_client

    spec = protocol.get_operation(Command.CMD_CLOCK_SYNC.value)
    assert spec is not None
    get_rpc_method = cast(Callable[[Any], Any], _private_attribute(transport, "_get_rpc_method"))
    assert get_rpc_method(spec) is None
    assert not await transport.send(Command.CMD_CLOCK_SYNC.value, pb.ClockSyncRequest(host_time_us=1))


async def test_invoke_rpc_completion_without_response_and_error_callback(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState, monkeypatch: Any
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    rpc_method = MagicMock()
    call = MagicMock()

    def complete_without_payload(
        _request: Any, *, on_next: Any, on_completed: Any, on_error: Any
    ) -> Any:
        on_completed(call, MagicMock(ok=MagicMock(return_value=True)))
        on_completed(call, MagicMock(ok=MagicMock(return_value=True)))
        return call

    rpc_method.invoke.side_effect = complete_without_payload
    invoke_rpc_with_retries = cast(
        Callable[[Any, Any, str, float], Coroutine[Any, Any, Any]],
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

    rpc_method.invoke.side_effect = RuntimeError("invoke error")
    result = await invoke_rpc_with_retries(
        rpc_method, pb.ClockSyncRequest(host_time_us=1), "ClockSync/Sync", 1.0
    )
    assert not result

    transport.max_attempts = 2
    call.cancel.side_effect = [RuntimeError("cancel race"), None]
    rpc_method.invoke.side_effect = None
    rpc_method.invoke.return_value = call
    monkeypatch.setattr("mcubridge.transport.serial.SERIAL_HANDSHAKE_BACKOFF_BASE", 0.0)
    result = await invoke_rpc_with_retries(
        rpc_method, pb.ClockSyncRequest(host_time_us=1), "ClockSync/Sync", 0.001
    )
    assert not result
    assert call.cancel.call_count == 2

    call.cancel.reset_mock()
    call.cancel.side_effect = ValueError("cancel on task abort")
    cancel_task = asyncio.create_task(
        invoke_rpc_with_retries(rpc_method, pb.ClockSyncRequest(host_time_us=1), "ClockSync/Sync", 10.0)
    )
    for _ in range(100):
        if active_rpc_calls:
            break
        await asyncio.sleep(0)
    active_rpc_calls.clear()
    cancel_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancel_task
    call.cancel.assert_called_once()


async def test_pigweed_gpio_server_stream_lifecycle_backpressure_and_rejection() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        transport = SerialTransport(config, state, service)
        service.serial = transport
        mcu_cipher = _install_link_cipher(config, state)
        state.connection_fsm.connect()
        mock_serial = MagicMock(spec=serial.Serial)
        mock_serial.is_open = True
        mock_serial.write = MagicMock(return_value=16)
        mock_serial.flush = MagicMock()
        transport.serial = mock_serial

        outgoing: list[bytes] = []

        def capture_rpc_packet(packet: bytes) -> None:
            outgoing.append(packet)
            transport.queue_rpc_packet(packet)

        transport.rpc_client = PwRpcClient(
            cast(Any, callback_client).Impl(),
            [PwRpcChannel(protocol.HDLC_ADDRESS_RPC, capture_rpc_packet)],
            protocol.PW_SERVICES,
        )

        mock_enqueue = AsyncMock()
        service.cloud_publisher = mock_enqueue

        # 1. Open server-streaming subscription via GpioService.subscribe_pin
        sub_res = await service.gpio.subscribe_pin(13, mode="INPUT_PULLUP", interval_ms=50, hysteresis=1)
        assert sub_res["status"] == "ok"
        assert len(outgoing) == 1
        req_pkt = pw_packets.decode(outgoing[-1])
        assert req_pkt.type == PacketType.REQUEST

        # 2. Emit 66 SERVER_STREAM PinUpdateEvent packets to exercise bounded queue + backpressure drop
        stream_queue = cast(asyncio.Queue[Any], _private_attribute(transport, "_stream_event_queue"))
        enqueue_fn = cast(Callable[[Any], None], _private_attribute(transport, "_enqueue_stream_event"))
        for idx in range(66):
            enqueue_fn(pb.PinUpdateEvent(pin=13, value=idx, timestamp_micros=1000 + idx))
        assert stream_queue.qsize() == 64
        assert _private_attribute(transport, "_stream_backpressure_drops") == 2

        drain_fn = cast(Callable[[], Coroutine[Any, Any, None]], _private_attribute(transport, "_drain_stream_events"))
        await drain_fn()
        assert stream_queue.empty()
        assert state.pin_events_count == 64

        # 3. Feed a live encrypted PacketType.SERVER_STREAM frame over HDLC
        evt_payload = pb.PinUpdateEvent(pin=13, value=1, timestamp_micros=777777).SerializeToString()
        stream_pkt = RpcPacket(
            type=PacketType.SERVER_STREAM,
            channel_id=req_pkt.channel_id,
            service_id=req_pkt.service_id,
            method_id=req_pkt.method_id,
            call_id=req_pkt.call_id,
            payload=evt_payload,
        )
        await transport.feed_data(
            ui_frame(
                protocol.HDLC_ADDRESS_RPC,
                mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, stream_pkt.SerializeToString()),
            )
        )
        assert state.pin_events_count == 65

        # 4. Feed PacketType.RESPONSE (status=OK) when MCU closes the stream cleanly
        close_pkt = RpcPacket(
            type=PacketType.RESPONSE,
            channel_id=req_pkt.channel_id,
            service_id=req_pkt.service_id,
            method_id=req_pkt.method_id,
            call_id=req_pkt.call_id,
            status=0,
        )
        await transport.feed_data(
            ui_frame(
                protocol.HDLC_ADDRESS_RPC,
                mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, close_pkt.SerializeToString()),
            )
        )
        assert _private_attribute(transport, "_stream_rpc_calls") == {}

        # 5. Reconnect and resubscribe active pin, then cancel stream explicitly
        restored = await service.gpio.resubscribe_pins()
        assert restored == 1
        assert transport.cancel_pin_stream()
        assert not transport.cancel_pin_stream()

        # 6. Immediate MCU rejection (RESOURCE_EXHAUSTED = 8) fails subscribe_pin
        def reject_immediately(packet: bytes) -> None:
            outgoing.append(packet)
            transport.queue_rpc_packet(packet)
            pkt = pw_packets.decode(packet)
            if pkt.type == PacketType.REQUEST:
                rej = RpcPacket(
                    type=PacketType.RESPONSE,
                    channel_id=pkt.channel_id,
                    service_id=pkt.service_id,
                    method_id=pkt.method_id,
                    call_id=pkt.call_id,
                    status=8,
                ).SerializeToString()
                rej_frame = ui_frame(
                    protocol.HDLC_ADDRESS_RPC,
                    mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, rej),
                )
                cast(Any, asyncio.get_running_loop()).call_soon(
                    asyncio.create_task,
                    transport.feed_data(rej_frame),
                )

        transport.rpc_client = PwRpcClient(
            cast(Any, callback_client).Impl(),
            [PwRpcChannel(protocol.HDLC_ADDRESS_RPC, reject_immediately)],
            protocol.PW_SERVICES,
        )
        rej_res = await service.gpio.subscribe_pin(99, enabled=True)
        assert rej_res["status"] == "error"
    finally:
        state.cleanup()


async def test_server_stream_error_and_reset_cleanup_branches(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    transport = SerialTransport(runtime_config, runtime_state, None)
    enqueue_fn = cast(Callable[[Any], None], _private_attribute(transport, "_enqueue_stream_event"))
    uninit_msg = MagicMock()
    uninit_msg.IsInitialized.return_value = False
    enqueue_fn(uninit_msg)
    assert runtime_state.serial_decode_errors == 1

    # Exercise stream invoke failure and on_error callback branches
    spec = protocol.get_operation(Command.CMD_PIN_SUBSCRIBE.value)
    assert spec is not None
    rpc_method = MagicMock()
    rpc_method.invoke.side_effect = RuntimeError("stream invoke error")
    invoke_stream = cast(
        Callable[[Any, Any, Any, str], Coroutine[Any, Any, bool]],
        _private_attribute(transport, "_invoke_server_stream_rpc"),
    )
    assert not await invoke_stream(
        rpc_method,
        pb.PinSubscribeRequest(pin=13, enabled=True),
        spec,
        "rpc.pb.GpioService/SubscribePins",
    )

    call = MagicMock()
    call.call_id = None
    call.cancel.side_effect = RuntimeError("cancel error")

    def trigger_on_error(_req: Any, *, on_next: Any, on_completed: Any, on_error: Any) -> Any:
        on_error(call, RuntimeError("stream aborted"))
        return call

    rpc_method.invoke.side_effect = trigger_on_error
    assert not await invoke_stream(
        rpc_method,
        pb.PinSubscribeRequest(pin=13, enabled=True),
        spec,
        "rpc.pb.GpioService/SubscribePins",
    )

    # Exercise reset() draining non-empty _stream_event_queue and cancel_pin_stream error handling
    stream_calls = cast(dict[tuple[int, int, int], Any], _private_attribute(transport, "_stream_rpc_calls"))
    stream_calls[(1, 2, 3)] = call
    enqueue_fn(pb.PinUpdateEvent(pin=1, value=1, timestamp_micros=1))
    await transport.reset()
    assert stream_calls == {}
