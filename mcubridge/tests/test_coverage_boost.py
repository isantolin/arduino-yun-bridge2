import asyncio
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from google.protobuf.message import Message as ProtobufMessage
from binascii import crc32
from typing import Any

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.frame import build_frame, parse_frame
from mcubridge.protocol import protocol
from mcubridge.security.security import (
    secure_zero,
    extract_nonce_counter,
    validate_nonce_counter,
    verify_crypto_integrity,
)
from mcubridge.transport.serial import SerialTransport
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.structures import PendingCommand
from pathlib import Path
import serialx

# Mock 'uci' globally for tests that import scripts directly
sys.modules["uci"] = MagicMock()


def test_frame_coverage_boost() -> None:

    # 1. Raw bytes payload
    frame = build_frame(command_id=10, sequence_id=1, payload=b"mockpayload")
    parsed = parse_frame(frame)
    assert parsed.payload == b"mockpayload"

    # 2. build_frame and parse_frame with session_key (AEAD encrypt / decrypt)
    key = b"\x00" * 32
    frame_enc = build_frame(command_id=10, sequence_id=2, payload=b"secretdata", session_key=key)
    parsed_enc = parse_frame(frame_enc, session_key=key)
    assert parsed_enc.payload == b"secretdata"

    # 3. parse_frame with wrong key (triggers InvalidTag)
    wrong_key = b"\x01" * 32
    with pytest.raises(ValueError, match="AEAD decryption failed"):
        parse_frame(frame_enc, session_key=wrong_key)

    # 4. parse_frame with unencrypted active field of message type
    req = pb.DigitalWrite(pin=13, value=1)
    frame_req = build_frame(command_id=5, sequence_id=3, payload=req)
    parsed_req = parse_frame(frame_req)
    assert parsed_req.envelope.WhichOneof("payload_type") == "digital_write"
    if isinstance(parsed_req.payload, ProtobufMessage):
        assert parsed_req.payload == req
    else:
        assert parsed_req.payload == req.SerializeToString()

    # 5. parse_frame with empty active field in payload_type
    envelope_empty = pb.RpcEnvelope(version=protocol.PROTOCOL_VERSION, command_id=10, sequence_id=4)
    body_empty = envelope_empty.SerializeToString()
    frame_empty = body_empty + (crc32(body_empty) & 0xFFFFFFFF).to_bytes(4, "little")
    parsed_empty = parse_frame(frame_empty)
    assert parsed_empty.payload == b""

    # 6. parse_frame with invalid protocol version
    envelope_bad_ver = pb.RpcEnvelope(version=999, command_id=10, sequence_id=5)
    body_bad_ver = envelope_bad_ver.SerializeToString()
    frame_bad_ver = body_bad_ver + (crc32(body_bad_ver) & 0xFFFFFFFF).to_bytes(4, "little")
    with pytest.raises(ValueError, match="Invalid protocol version"):
        parse_frame(frame_bad_ver)

    # 7. parse_frame with failed protobuf envelope parse
    body_bad_proto = b"\xff\xff\xff\xff"
    frame_bad_proto = body_bad_proto + (crc32(body_bad_proto) & 0xFFFFFFFF).to_bytes(4, "little")
    with pytest.raises(ValueError, match="Failed to parse Protobuf envelope"):
        parse_frame(frame_bad_proto)


def test_security_coverage_boost() -> None:
    # 1. secure_zero propagates exceptions (security violation = explicit error [SIL-2])
    with patch("ctypes.memset", side_effect=TypeError("mock error")):
        with pytest.raises(TypeError, match="mock error"):
            secure_zero(bytearray(b"test"))

    # 2. extract_nonce_counter invalid length
    with pytest.raises(ValueError, match="Nonce must be 12 bytes"):
        extract_nonce_counter(b"too-short")

    # 3. validate_nonce_counter length check fail
    ok, _ = validate_nonce_counter(b"too-short", 0)
    assert not ok

    # 4. verify_crypto_integrity KAT failures
    with patch("mcubridge.security.security.hashes.Hash") as mock_hash:
        mock_hash.return_value.finalize.return_value = b"invalid"
        assert not verify_crypto_integrity()

    with patch("mcubridge.security.security.hmac.HMAC") as mock_hmac:
        mock_hmac.return_value.finalize.return_value = b"invalid"
        assert not verify_crypto_integrity()

    with patch("mcubridge.security.security.ChaCha20Poly1305", side_effect=ValueError):
        assert not verify_crypto_integrity()


@pytest.mark.asyncio
async def test_serial_transport_coverage_boost(tmp_path: Path) -> None:
    # Pyright ahora deducirá correctamente que test_root y test_spool son objetos Path
    test_root = tmp_path / "yun_files"
    test_spool = tmp_path / "spool"
    test_root.mkdir()
    test_spool.mkdir()

    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/testport",
        serial_fallback_threshold=2,
        serial_baud=115200,
        serial_safe_baud=9600,
        # Forzar aislamiento absoluto de archivos DBM en el hilo actual
        file_system_root=str(test_root),
        cloud_spool_dir=str(test_spool),
    )

    # Ahora la base de datos se creará de forma aislada y determinista
    state = create_runtime_state(config)
    state.serial_tx_allowed.set()

    # McuService mock setup: register_serial_sender must be a synchronous mock
    service = MagicMock()
    service.on_serial_connected = AsyncMock()
    service.on_serial_disconnected = AsyncMock()
    service.handle_mcu_frame = AsyncMock()

    transport: Any = SerialTransport(config, state, service)

    class BadSerial:
        @property
        def baudrate(self) -> int:
            return 9600

        @baudrate.setter
        def baudrate(self, _val: int) -> None:
            raise ValueError("bad baud")

    mock_serial = MagicMock()
    mock_serial.transport = MagicMock()
    mock_serial.transport.serial = BadSerial()
    transport.serial = mock_serial
    with pytest.raises(RuntimeError, match="UART access failed"):
        transport._switch_local_baudrate(9600)

    # 3. reset when pending current command
    pending = PendingCommand(command_id=1, expected_resp_ids={2})
    transport._current = pending
    await transport.reset()
    assert pending.completion.is_set()
    assert transport._current is None

    # 4. stop when serial exists
    mock_serial_stop = AsyncMock()
    transport.serial = mock_serial_stop
    await transport.stop()
    mock_serial_stop.close.assert_called_once()

    # 5. _read_loop with IncompleteReadError and LimitOverrunError
    transport._stop_event.clear()
    import serialx

    mock_serial_reader = AsyncMock(spec=serialx.AsyncSerial)
    mock_serial_reader.readuntil.side_effect = [
        asyncio.LimitOverrunError("limit", 0),
        asyncio.IncompleteReadError(b"partial", 10),
    ]
    try:
        await transport._read_loop(mock_serial_reader)
        mock_serial_reader.read.assert_awaited_once_with(1024)
    except AssertionError as e:
        with open(Path(__file__).parent.parent.parent / ".tmp_calls.txt", "w") as f:
            f.write(f"mock_reader.mock_calls: {mock_serial_reader.mock_calls}\n")
            f.write(f"mock_reader.readuntil.mock_calls: {mock_serial_reader.readuntil.mock_calls}\n")
            f.write(f"mock_reader.read.mock_calls: {mock_serial_reader.read.mock_calls}\n")
            f.write(f"stop_event: {transport._stop_event.is_set()}\n")
            f.write(f"AssertionError: {e}\n")
        raise

    # 6. _read_loop with OSError
    transport._stop_event.clear()
    mock_serial_reader2 = AsyncMock(spec=serialx.AsyncSerial)
    mock_serial_reader2.readuntil.side_effect = OSError("read error")
    await transport._read_loop(mock_serial_reader2)

    # 7. _process_packet with malformed packet (triggers _check_baudrate_fallback)
    transport._consecutive_crc_errors = 1
    with patch.object(transport, "_negotiate_baudrate", return_value=True) as mock_negotiate:
        await transport._process_packet(b"invalid cobs data")
        assert transport._consecutive_crc_errors == 0
        mock_negotiate.assert_awaited_once_with(9600)

    # 8. _correlate_frame with ack targeting non-pending command or invalid ACK payload
    transport._current = PendingCommand(command_id=10, expected_resp_ids={11})
    # Valid ACK for a different command id should not set ack_received
    transport._correlate_frame(Status.ACK.value, pb.AckPacket(command_id=999).SerializeToString())
    assert not transport._current.ack_received

    # Invalid ACK payload logs a warning and falls back to pending command_id, which sets ack_received
    transport._correlate_frame(Status.ACK.value, b"invalid ack pb")
    assert transport._current.ack_received

    # 9. send_raw when serial is None
    transport.serial = None
    assert not await transport.send_raw(1, b"")

    # 10. send_raw with write error
    mock_serial2 = AsyncMock()
    mock_serial2.write.side_effect = OSError("write error")
    transport.serial = mock_serial2
    assert not await transport.send_raw(1, b"")


@pytest.mark.asyncio
async def test_daemon_coverage_boost(tmp_path: Path) -> None:
    from mcubridge.services.runtime import BridgeService

    # Estructura del sandbox completamente tipada
    test_root = tmp_path / "yun_files"
    test_spool = tmp_path / "spool"
    test_root.mkdir()
    test_spool.mkdir()

    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/testport",
        cloud_enabled=False,
        watchdog_enabled=True,
        metrics_enabled=False,
        bridge_summary_interval=1.0,
        bridge_handshake_interval=1.0,
        file_system_root=str(test_root),
        cloud_spool_dir=str(test_spool),
    )

    state = create_runtime_state(config)
    serial = SerialTransport(config, state, None)
    daemon = BridgeService(config, state, serial)
    serial.service = daemon
    await daemon.run_cloud()

    config_cloud = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/testport",
        cloud_enabled=True,
        cloud_host="localhost",
        cloud_port=8443,
        file_system_root=str(test_root / "cloud_files"),
        cloud_spool_dir=str(test_spool / "cloud_spool"),
    )

    (test_root / "cloud_files").mkdir()
    (test_spool / "cloud_spool").mkdir()

    state_cloud = create_runtime_state(config_cloud)
    serial_cloud = SerialTransport(config_cloud, state_cloud, None)
    daemon_cloud: Any = BridgeService(config_cloud, state_cloud, serial_cloud)
    serial_cloud.service = daemon_cloud

    mock_channel = MagicMock()
    mock_channel.close = MagicMock()

    mock_stream = MagicMock()
    mock_stream.send_message = AsyncMock()

    envelope = pb.CloudEnvelope(
        sequence_id=1,
        command_request=pb.CommandRequest(
            command_path="system/version/get",
            payload=b"",
        ),
    )

    async def mock_aiter():
        yield envelope

    def mock_aiter_method(s: Any) -> Any:
        return mock_aiter()

    mock_stream.__aiter__ = mock_aiter_method

    mock_open = MagicMock()
    mock_open.__aenter__ = AsyncMock(return_value=mock_stream)
    mock_open.__aexit__ = AsyncMock(return_value=None)

    mock_stub = MagicMock()
    mock_stub.Session = MagicMock()
    mock_stub.Session.open = MagicMock(return_value=mock_open)

    with (
        patch("mcubridge.services.runtime.Channel", return_value=mock_channel),
        patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub),
    ):
        try:
            await daemon_cloud.connect_cloud_session(None)
        except ConnectionResetError:
            pass

    # Connect Cloud session exception path
    mock_open_fail = MagicMock()
    mock_open_fail.__aenter__ = AsyncMock(side_effect=OSError("connection reset"))
    mock_open_fail.__aexit__ = AsyncMock(return_value=None)

    mock_stub_fail = MagicMock()
    mock_stub_fail.Session = MagicMock()
    mock_stub_fail.Session.open = MagicMock(return_value=mock_open_fail)

    with (
        patch("mcubridge.services.runtime.Channel", return_value=mock_channel),
        patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub_fail),
    ):
        with pytest.raises(OSError):
            await daemon_cloud.connect_cloud_session(None)

    # 3. supervise with fatal exceptions
    from mcubridge.services.handshake import SerialHandshakeFatal

    async def mock_fatal_factory():
        raise SerialHandshakeFatal("fatal")

    with pytest.raises(SerialHandshakeFatal):
        await daemon_cloud.supervise("test-fatal", mock_fatal_factory, fatal_exceptions=(SerialHandshakeFatal,))

    # 4. daemon.run with ExceptionGroup
    with patch("asyncio.TaskGroup.__aexit__", side_effect=ExceptionGroup("tasks", [OSError("task error")])):
        state_opt = create_runtime_state(config)
        serial_opt = SerialTransport(config, state_opt, None)
        daemon_opt = BridgeService(config, state_opt, serial_opt)
        serial_opt.service = daemon_opt
        with pytest.raises(ExceptionGroup):
            await daemon_opt.run()


def test_daemon_main_coverage() -> None:
    from mcubridge.daemon import app as main
    from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET

    # 1. verify_crypto_integrity fails in main
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=False):
        with patch("mcubridge.daemon.logger") as mock_logger:
            with pytest.raises(SystemExit) as excinfo:
                main([])
            assert excinfo.value.code == 1
            mock_logger.critical.assert_called_with("CRYPTOGRAPHIC INTEGRITY CHECK FAILED! Aborting for security.")

    # 2. default shared secret warning block
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/testport",
        serial_shared_secret=DEFAULT_SERIAL_SHARED_SECRET,
    )
    with patch("mcubridge.daemon.load_runtime_config", return_value=config):
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
            with patch("asyncio.Runner") as mock_runner:

                def run_side_effect(coro: Any) -> Any:
                    coro.close()
                    raise KeyboardInterrupt

                mock_runner.return_value.__enter__.return_value.run.side_effect = run_side_effect
                main([])

    # 3. Exception group in main
    with patch("mcubridge.daemon.load_runtime_config"):
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
            with patch("asyncio.Runner") as mock_runner:

                def run_side_effect_eg(coro: Any) -> Any:
                    coro.close()
                    raise ExceptionGroup("group", [OSError("os error")])

                mock_runner.return_value.__enter__.return_value.run.side_effect = run_side_effect_eg
                with pytest.raises(SystemExit) as excinfo:
                    main([])
                assert excinfo.value.code == 1

    # 4. Help and version
    with pytest.raises(SystemExit):
        main(["--help"])


@pytest.mark.asyncio
async def test_handshake_coverage_boost() -> None:
    from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing

    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/testport",
        serial_handshake_fatal_failures=2,
        serial_shared_secret=b"secret1234",
    )
    state = create_runtime_state(config)
    send_frame = AsyncMock(return_value=True)
    enqueue_cloud = AsyncMock()
    acknowledge_frame = AsyncMock()

    manager = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=derive_serial_timing(config),
        send_frame=send_frame,
        enqueue_cloud=enqueue_cloud,
        acknowledge_frame=acknowledge_frame,
    )

    # 1. handle_link_sync_resp without pending nonce
    state.link_handshake_nonce = None
    assert not await manager.handle_link_sync_resp(1, b"")

    # 2. handle_link_sync_resp throttled due to rate limit
    state.link_handshake_nonce = b"\x00" * 16
    state.handshake_rate_until = 99999999.0
    with patch("time.monotonic", return_value=0.0):
        assert not await manager.handle_link_sync_resp(1, b"")

    # 3. handle_link_sync_resp protobuf decode failure
    state.handshake_rate_until = 0.0
    assert not await manager.handle_link_sync_resp(1, b"bad protobuf")

    # 4. handle_link_sync_resp mismatch tag
    state.link_handshake_nonce = b"\x00" * 16
    state.link_expected_tag = b"correct_tag"
    sync_resp_pb = pb.LinkSync(nonce=b"\x00" * 16, tag=b"wrong_tag_16_bytes")
    assert not await manager.handle_link_sync_resp(1, sync_resp_pb.SerializeToString())

    # 5. handle_handshake_failure fatal
    state.handshake_failure_streak = 5
    await manager.handle_handshake_failure("sync_auth_mismatch", detail="test_detail")
    assert state.handshake_fatal_count > 0


@pytest.mark.asyncio
async def test_local_bridge_service_coverage(tmp_path: Path) -> None:
    from mcubridge.services.runtime import LocalBridgeService, BridgeService, BridgeRequest
    from grpclib.server import Stream

    test_root = tmp_path / "yun_files"
    test_spool = tmp_path / "spool"
    test_root.mkdir(exist_ok=True)
    test_spool.mkdir(exist_ok=True)
    config = RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
        file_system_root=str(test_root),
        cloud_spool_dir=str(test_spool),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    runtime_service = BridgeService(config, state, mock_serial)
    service = LocalBridgeService(runtime_service)

    try:
        # 1. Publish without correlation_data
        mock_stream_1 = AsyncMock(spec=Stream)
        req_pub = pb.CloudQueuedPublish(topic_name="br/d/13", payload=b"payload")
        mock_stream_1.recv_message.return_value = req_pub

        runtime_service.handle_request = AsyncMock()

        await service.Publish(mock_stream_1)

        runtime_service.handle_request.assert_awaited_once()
        mock_stream_1.send_message.assert_awaited_once_with(pb.CloudQueuedPublish())

        # 2. Publish with correlation_data (success path)
        mock_stream_2 = AsyncMock(spec=Stream)
        correlation_id = b"corr12345678"
        req_pub_corr = pb.CloudQueuedPublish(topic_name="br/d/13", payload=b"payload", correlation_data=correlation_id)
        mock_stream_2.recv_message.return_value = req_pub_corr

        async def side_effect_handle(req: BridgeRequest) -> None:
            queue = runtime_service.ipc_requests.get(correlation_id)
            if queue:
                await queue.put(pb.CloudQueuedPublish(topic_name="response"))

        runtime_service.handle_request.side_effect = side_effect_handle

        await service.Publish(mock_stream_2)
        mock_stream_2.send_message.assert_awaited_with(pb.CloudQueuedPublish(topic_name="response"))

        # 3. Publish with correlation_data (timeout path)
        mock_stream_3 = AsyncMock(spec=Stream)
        req_pub_corr_timeout = pb.CloudQueuedPublish(
            topic_name="br/d/13", payload=b"payload", correlation_data=b"timeout_corr"
        )
        mock_stream_3.recv_message.return_value = req_pub_corr_timeout

        with patch("asyncio.timeout", side_effect=TimeoutError):
            await service.Publish(mock_stream_3)
        mock_stream_3.send_message.assert_awaited_with(pb.CloudQueuedPublish())

        # 4. SubscribeConsole (success path)
        mock_stream_4 = AsyncMock(spec=Stream)
        mock_stream_4.recv_message.return_value = pb.SubscribeRequest()

        async def mock_send(msg: pb.CloudQueuedPublish) -> None:
            raise asyncio.CancelledError()

        mock_stream_4.send_message.side_effect = mock_send

        async def feed_console() -> None:
            await asyncio.sleep(0.05)
            assert len(runtime_service.console_queues) == 1
            queue = runtime_service.console_queues[0]
            await queue.put(pb.CloudQueuedPublish(topic_name="console_out"))

        feed_task = asyncio.create_task(feed_console())
        try:
            await service.SubscribeConsole(mock_stream_4)
        except asyncio.CancelledError:
            pass
        await feed_task
        assert len(runtime_service.console_queues) == 0
    finally:
        runtime_service.cleanup()


@pytest.mark.asyncio
async def test_cloud_incoming_worker_coverage(tmp_path: Path) -> None:
    from mcubridge.services.runtime import BridgeService, BridgeRequest

    test_root = tmp_path / "yun_files"
    test_spool = tmp_path / "spool"
    test_root.mkdir(exist_ok=True)
    test_spool.mkdir(exist_ok=True)

    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/testport",
        file_system_root=str(test_root),
        cloud_spool_dir=str(test_spool),
    )
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    daemon = BridgeService(config, state, mock_serial)

    try:
        # 1. Start worker in task
        task = asyncio.create_task(getattr(daemon, "_cloud_incoming_worker")())

        # 2. Push message that succeeds
        daemon.handle_request = AsyncMock()
        req = BridgeRequest(topic="br/d/13", payload=b"")
        getattr(daemon, "_cloud_incoming_queue").put_nowait(req)

        # 3. Push message that fails handle_request to cover error path
        daemon.handle_request.side_effect = ValueError("test error")
        getattr(daemon, "_cloud_incoming_queue").put_nowait(req)

        await asyncio.sleep(0.05)

        # 4. Cancel worker to cover CancelledError path
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        daemon.cleanup()


@pytest.mark.asyncio
async def test_daemon_full_run_coverage(tmp_path: Path) -> None:
    from mcubridge.services.runtime import BridgeService

    test_root = tmp_path / "yun_files"
    test_spool = tmp_path / "spool"
    test_root.mkdir(exist_ok=True)
    test_spool.mkdir(exist_ok=True)

    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/testport",
        watchdog_enabled=True,
        metrics_enabled=True,
        bridge_summary_interval=0.1,
        bridge_handshake_interval=0.1,
        file_system_root=str(test_root),
        cloud_spool_dir=str(test_spool),
    )
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    mock_serial.run = AsyncMock()

    daemon = BridgeService(config, state, mock_serial)
    daemon.run_ipc_server = AsyncMock()
    daemon.run_cloud = AsyncMock()

    try:
        with patch("mcubridge.services.runtime.PrometheusExporter") as mock_exporter_cls:
            mock_exporter = MagicMock()
            mock_exporter.run = AsyncMock()
            mock_exporter_cls.return_value = mock_exporter

            task = asyncio.create_task(daemon.run())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        daemon.cleanup()


@pytest.mark.asyncio
async def test_serial_transport_extra_coverage_boost(tmp_path: Path) -> None:
    from mcubridge.transport.serial import SerialTransport
    from mcubridge.services.handshake import SerialHandshakeFatal

    test_root = tmp_path / "yun_files"
    test_spool = tmp_path / "spool"
    test_root.mkdir(exist_ok=True)
    test_spool.mkdir(exist_ok=True)

    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/testport",
        serial_fallback_threshold=2,
        serial_baud=115200,
        serial_safe_baud=9600,
        file_system_root=str(test_root),
        cloud_spool_dir=str(test_spool),
    )
    state = create_runtime_state(config)

    try:
        # 1. _switch_local_baudrate when self.serial is None
        service = MagicMock()
        service.on_serial_connected = AsyncMock()
        service.on_serial_disconnected = AsyncMock()
        transport = SerialTransport(config, state, service)
        assert transport.serial is None
        getattr(transport, "_switch_local_baudrate")(9600)

        # 2. _toggle_dtr when exception occurs
        mock_serial = AsyncMock()
        mock_serial.set_modem_pins = AsyncMock(side_effect=OSError("modem error"))
        transport.serial = mock_serial
        await getattr(transport, "_toggle_dtr")()

        # 3. ConnectionError in _connect_and_run when baudrate negotiation fails
        setattr(transport, "_toggle_dtr", AsyncMock())
        setattr(transport, "_read_loop", AsyncMock())
        setattr(transport, "_negotiate_baudrate", AsyncMock(return_value=False))

        with patch("serialx.AsyncSerial", return_value=mock_serial):
            with pytest.raises(ConnectionError, match="Baudrate negotiation failed"):
                await getattr(transport, "_connect_and_run")()

        # 4. ConnectionError in _connect_and_run when read task is done first
        setattr(transport, "_negotiate_baudrate", AsyncMock(return_value=True))

        async def mock_read_loop(serial: serialx.AsyncSerial) -> None:
            pass

        setattr(transport, "_read_loop", mock_read_loop)

        with patch("serialx.AsyncSerial", return_value=mock_serial):
            with pytest.raises(ConnectionError, match="Serial connection lost"):
                await getattr(transport, "_connect_and_run")()

        # 5. Exception during disconnect cleanup in _connect_and_run
        service.on_serial_disconnected = AsyncMock(side_effect=TypeError("cleanup fail"))
        with patch("serialx.AsyncSerial", return_value=mock_serial):
            with pytest.raises(ConnectionError):
                await getattr(transport, "_connect_and_run")()

        # 6. SerialTransport.run handling SerialHandshakeFatal
        setattr(transport, "_connect_and_run", AsyncMock(side_effect=SerialHandshakeFatal("fatal")))
        with pytest.raises(SerialHandshakeFatal):
            await transport.run()

        # 7. SerialTransport.run handling CancelledError
        setattr(transport, "_connect_and_run", AsyncMock(side_effect=asyncio.CancelledError()))
        await transport.run()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_structures_extra_coverage_boost(tmp_path: Path) -> None:
    from mcubridge.protocol.structures import validate_config, get_ssl_context, allows_topic
    from mcubridge.protocol import mcubridge_pb2 as pb

    # 1. allows_topic default return False
    auth = pb.TopicAuthorization()
    assert not allows_topic(auth, "unknown_topic", "unknown_action")

    # 2. validate_config exceptions
    cfg = pb.RuntimeConfig(topic_prefix="")
    with pytest.raises(ValueError, match="topic_prefix must contain at least one segment"):
        validate_config(cfg)

    cfg = pb.RuntimeConfig(
        topic_prefix="br",
        serial_response_timeout=1,
        serial_retry_timeout=1,
    )
    with pytest.raises(ValueError, match="serial_response_timeout must be at least 2x serial_retry_timeout"):
        validate_config(cfg)

    cfg = pb.RuntimeConfig(
        topic_prefix="br",
        serial_response_timeout=10,
        serial_retry_timeout=2,
        watchdog_enabled=True,
        watchdog_interval=0.1,
    )
    with pytest.raises(ValueError, match="watchdog_interval must be >= 0.5s when enabled"):
        validate_config(cfg)

    cfg = pb.RuntimeConfig(
        topic_prefix="br",
        serial_response_timeout=10,
        serial_retry_timeout=2,
        watchdog_enabled=False,
        serial_shared_secret=b"",
    )
    with pytest.raises(ValueError, match="serial_shared_secret must be configured"):
        validate_config(cfg)

    cfg.serial_shared_secret = b"changeme123"
    with pytest.raises(ValueError, match="serial_shared_secret placeholder is insecure"):
        validate_config(cfg)

    cfg.serial_shared_secret = b"aaaa"
    with pytest.raises(ValueError, match="serial_shared_secret must contain at least four distinct bytes"):
        validate_config(cfg)

    cfg.serial_shared_secret = b"abcd"
    cfg.file_storage_quota_bytes = 10
    cfg.file_write_max_bytes = 20
    with pytest.raises(
        ValueError, match="file_storage_quota_bytes must be greater than or equal to file_write_max_bytes"
    ):
        validate_config(cfg)

    cfg.file_storage_quota_bytes = 100
    cfg.file_write_max_bytes = 20
    cfg.mailbox_queue_bytes_limit = 10
    cfg.mailbox_queue_limit = 20
    with pytest.raises(
        ValueError, match="mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit"
    ):
        validate_config(cfg)

    cfg.mailbox_queue_bytes_limit = 100
    cfg.mailbox_queue_limit = 10
    cfg.allow_non_tmp_paths = False
    cfg.cloud_spool_dir = "/usr/bin/spool"
    cfg.file_system_root = "/tmp"
    with pytest.raises(ValueError, match="FLASH PROTECTION: cloud_spool_dir .* must be in volatile storage"):
        validate_config(cfg)

    cfg.cloud_spool_dir = "/tmp"
    cfg.file_system_root = "/usr/bin/fs"
    with pytest.raises(ValueError, match="FLASH PROTECTION: file_system_root .* must be in volatile storage"):
        validate_config(cfg)

    # 3. get_ssl_context
    cfg_ssl = pb.RuntimeConfig(cloud_tls=True, cloud_cafile=str(tmp_path / "nonexistent_ca.crt"))
    with pytest.raises(RuntimeError, match="Cloud TLS CA file missing"):
        get_ssl_context(cfg_ssl)

    cfg_ssl2 = pb.RuntimeConfig(cloud_tls=True, cloud_cafile="", cloud_certfile="cert.pem")
    with pytest.raises(RuntimeError, match="TLS setup failed"):
        get_ssl_context(cfg_ssl2)


@pytest.mark.asyncio
async def test_handshake_manager_extra_coverage() -> None:
    from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing, HandshakeState
    from google.protobuf.message import Message as ProtobufMessage

    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/testport",
        serial_handshake_fatal_failures=2,
        serial_shared_secret=b"secret1234",
    )
    state = create_runtime_state(config)

    try:
        send_frame = AsyncMock(return_value=True)
        enqueue_cloud = AsyncMock()
        acknowledge_frame = AsyncMock()

        manager = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=derive_serial_timing(config),
            send_frame=send_frame,
            enqueue_cloud=enqueue_cloud,
            acknowledge_frame=acknowledge_frame,
        )

        # 1. Test HandshakeState transitions
        getattr(manager, "_set_fsm_state")(HandshakeState.SYNCHRONIZED)
        assert manager.fsm_state == HandshakeState.SYNCHRONIZED
        getattr(manager, "_set_fsm_state")(HandshakeState.UNSYNCHRONIZED)
        assert manager.fsm_state == HandshakeState.UNSYNCHRONIZED

        # 2. _synchronize_attempt reset_ok is False
        send_frame.return_value = False
        assert not await getattr(manager, "_synchronize_attempt")()

        # 3. _synchronize_attempt sync_ok is False
        send_frame.side_effect = [True, False]
        assert not await getattr(manager, "_synchronize_attempt")()

        # 4. _synchronize_attempt fsm_state == FAULT early exit
        async def mock_send_set_fault(
            command_id: int,
            payload: bytes | ProtobufMessage,
            seq_id: int | None = None,
        ) -> bool:
            manager.fsm_state = HandshakeState.FAULT
            return True

        setattr(manager, "_send_frame", mock_send_set_fault)
        assert not await getattr(manager, "_synchronize_attempt")()

        # 5. _synchronize_attempt timeout (confirmed is False)
        setattr(manager, "_send_frame", AsyncMock(return_value=True))
        manager.fsm_state = HandshakeState.UNSYNCHRONIZED
        setattr(manager, "_wait_for_link_sync_confirmation", AsyncMock(return_value=False))
        assert not await getattr(manager, "_synchronize_attempt")()

        # 6. RetryError in synchronize
        setattr(manager, "_synchronize_attempt", AsyncMock(return_value=False))
        with patch.object(manager, "_fatal_threshold", 1):
            assert not await manager.synchronize()
            assert manager.fsm_state == HandshakeState.FAULT

        # 7. _fetch_capabilities success and fail paths
        manager.fsm_state = HandshakeState.SYNCHRONIZED

        async def mock_send(
            command_id: int,
            payload: bytes | ProtobufMessage,
            seq_id: int | None = None,
        ) -> bool:
            future = getattr(manager, "_capabilities_future")
            if future and not future.done():
                cap = pb.Capabilities()
                future.set_result(cap.SerializeToString())
            return True

        setattr(manager, "_send_frame", mock_send)

        try:
            assert await getattr(manager, "_fetch_capabilities")()
        except Exception:
            raise

        async def mock_send_fail(
            command_id: int,
            payload: bytes | ProtobufMessage,
            seq_id: int | None = None,
        ) -> bool:
            return False

        setattr(manager, "_send_frame", mock_send_fail)

        async def mock_async_sleep(delay: float) -> None:
            pass

        with patch("asyncio.sleep", mock_async_sleep):
            assert not await getattr(manager, "_fetch_capabilities")()
    finally:
        state.cleanup()
