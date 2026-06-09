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

# Mock 'uci' globally for tests that import scripts directly
sys.modules["uci"] = MagicMock()


def test_frame_coverage_boost() -> None:
    # 1. Message descriptor name not in map
    mock_msg = MagicMock(spec=ProtobufMessage)
    mock_msg.DESCRIPTOR.name = "NonExistentName"
    mock_msg.SerializeToString.return_value = b"mockpayload"
    frame = build_frame(command_id=10, sequence_id=1, payload=mock_msg)
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
    # 1. secure_zero exception path using mock patch
    with patch("ctypes.memset", side_effect=TypeError("mock error")):
        secure_zero(bytearray(b"test"))

    # 2. extract_nonce_counter invalid length
    with pytest.raises(ValueError, match="Nonce must be 12 bytes"):
        extract_nonce_counter(b"too-short")

    # 3. validate_nonce_counter length check fail
    ok, _ = validate_nonce_counter(b"too-short", 0)
    assert ok is False

    # 4. verify_crypto_integrity KAT failures
    with patch("hashlib.sha256") as mock_sha:
        mock_sha.return_value.hexdigest.return_value = "invalid"
        assert verify_crypto_integrity() is False

    with patch("hmac.new") as mock_hmac:
        mock_hmac.return_value.hexdigest.return_value = "invalid"
        assert verify_crypto_integrity() is False

    with patch("mcubridge.security.security.ChaCha20Poly1305", side_effect=ValueError):
        assert verify_crypto_integrity() is False


@pytest.mark.asyncio
async def test_serial_transport_coverage_boost(tmp_path: Path) -> None:
    # Pyright ahora deducirá correctamente que test_root y test_spool son objetos Path
    test_root = tmp_path / "yun_files"
    test_spool = tmp_path / "spool"
    test_root.mkdir()
    test_spool.mkdir()

    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/testport",
        serial_fallback_threshold=2,
        serial_baud=115200,
        serial_safe_baud=9600,
        # Forzar aislamiento absoluto de archivos DBM en el hilo actual
        file_system_root=str(test_root),
        mqtt_spool_dir=str(test_spool),
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

    # 1. _active_transport raises when writer is None
    with pytest.raises(RuntimeError, match="Serial writer inactive"):
        transport._active_transport()

    # 2. _switch_local_baudrate raises when transport throws error
    class BadSerial:
        @property
        def baudrate(self) -> int:
            return 9600

        @baudrate.setter
        def baudrate(self, _val: int) -> None:
            raise ValueError("bad baud")

    mock_writer = MagicMock()
    mock_writer.is_closing.return_value = False
    mock_active = MagicMock()
    mock_active.serial = BadSerial()
    mock_writer.transport = mock_active
    transport.writer = mock_writer
    with pytest.raises(RuntimeError, match="UART access failed"):
        transport._switch_local_baudrate(9600)

    # 3. reset when pending current command
    pending = PendingCommand(command_id=1, expected_resp_ids={2})
    transport._current = pending
    await transport.reset()
    assert pending.completion.is_set()
    assert transport._current is None

    # 4. stop when writer exists
    mock_writer_stop = MagicMock()
    transport.writer = mock_writer_stop
    await transport.stop()
    mock_writer_stop.close.assert_called_once()

    # 5. _read_loop with IncompleteReadError and LimitOverrunError
    transport._stop_event.clear()
    mock_reader = AsyncMock()
    mock_reader.readuntil.side_effect = [
        asyncio.LimitOverrunError("limit", 0),
        asyncio.IncompleteReadError(b"partial", 10),
    ]
    try:
        await transport._read_loop(mock_reader)
        mock_reader.read.assert_awaited_once_with(1024)
    except AssertionError as e:
        with open("/home/ignaciosantolin/arduino-yun-bridge2/.tmp_calls.txt", "w") as f:
            f.write(f"mock_reader.mock_calls: {mock_reader.mock_calls}\n")
            f.write(f"mock_reader.readuntil.mock_calls: {mock_reader.readuntil.mock_calls}\n")
            f.write(f"mock_reader.read.mock_calls: {mock_reader.read.mock_calls}\n")
            f.write(f"stop_event: {transport._stop_event.is_set()}\n")
            f.write(f"AssertionError: {e}\n")
        raise

    # 6. _read_loop with OSError
    transport._stop_event.clear()
    mock_reader2 = AsyncMock()
    mock_reader2.readuntil.side_effect = OSError("read error")
    await transport._read_loop(mock_reader2)

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

    # 9. send_raw when writer is None
    transport.writer = None
    assert await transport.send_raw(1, b"") is False

    # 10. send_raw with write error
    mock_writer2 = MagicMock()
    mock_writer2.write.side_effect = OSError("write error")
    transport.writer = mock_writer2
    assert await transport.send_raw(1, b"") is False


@pytest.mark.asyncio
async def test_daemon_coverage_boost(tmp_path: Path) -> None:
    from mcubridge.daemon import BridgeDaemon, app

    # Estructura del sandbox completamente tipada
    test_root = tmp_path / "yun_files"
    test_spool = tmp_path / "spool"
    test_root.mkdir()
    test_spool.mkdir()

    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/testport",
        mqtt_enabled=False,
        watchdog_enabled=True,
        metrics_enabled=False,
        bridge_summary_interval=1.0,
        bridge_handshake_interval=1.0,
        file_system_root=str(test_root),
        mqtt_spool_dir=str(test_spool),
    )

    daemon = BridgeDaemon(config)
    await daemon.run_mqtt()

    config_mqtt = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/testport",
        mqtt_enabled=True,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user="",
        file_system_root=str(test_root / "mqtt_files"),
        mqtt_spool_dir=str(test_spool / "mqtt_spool"),
    )
    
    (test_root / "mqtt_files").mkdir()
    (test_spool / "mqtt_spool").mkdir()

    daemon_mqtt: Any = BridgeDaemon(config_mqtt)

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client

    class AsyncIter:
        def __init__(self, items: list[Any]) -> None:
            self.items = items

        def __aiter__(self) -> "AsyncIter":
            return self

        async def __anext__(self) -> Any:
            if not self.items:
                raise StopAsyncIteration
            return self.items.pop(0)

    msg = MagicMock()
    msg.topic = "br/mcu/cmd"
    msg.payload = b"msg"
    mock_client.messages = AsyncIter([msg])

    with patch("aiomqtt.Client", return_value=mock_client):
        await daemon_mqtt.connect_mqtt_session(None)
        assert daemon_mqtt.service._mqtt_client is None

    # Connect MQTT session ExceptionGroup path
    mock_client2 = AsyncMock()
    mock_client2.__aenter__.return_value = mock_client2

    class ExceptionIter:
        def __aiter__(self) -> "ExceptionIter":
            return self

        async def __anext__(self) -> Any:
            raise ExceptionGroup("mqtt group", [ValueError("group error")])

    mock_client2.messages = ExceptionIter()
    with patch("aiomqtt.Client", return_value=mock_client2):
        with pytest.raises(ExceptionGroup):
            await daemon_mqtt.connect_mqtt_session(None)

    # 3. supervise with fatal exceptions
    from mcubridge.services.handshake import SerialHandshakeFatal

    async def mock_fatal_factory():
        raise SerialHandshakeFatal("fatal")

    with pytest.raises(SerialHandshakeFatal):
        await daemon_mqtt.supervise("test-fatal", mock_fatal_factory, fatal_exceptions=(SerialHandshakeFatal,))

    # 4. daemon.run with ExceptionGroup
    with patch("asyncio.TaskGroup.__aexit__", side_effect=ExceptionGroup("tasks", [OSError("task error")])):
        daemon_opt = BridgeDaemon(config)
        with pytest.raises(ExceptionGroup):
            await daemon_opt.run()

    # 5. app entry point CLI
    with patch("mcubridge.daemon.main") as mock_main:
        with pytest.raises(SystemExit):
            app(["--help"])
        app([])
        mock_main.assert_called_once()


def test_daemon_main_coverage() -> None:
    from mcubridge.daemon import main
    from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET

    # 1. verify_crypto_integrity fails in main
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=False):
        with patch("mcubridge.daemon.logger") as mock_logger:
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 1
            mock_logger.critical.assert_called_with("CRYPTOGRAPHIC INTEGRITY CHECK FAILED! Aborting for security.")

    # 2. default shared secret warning block
    config = RuntimeConfig(
        mqtt_topic="br",
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
                main()

    # 3. Exception group in main
    with patch("mcubridge.daemon.load_runtime_config"):
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
            with patch("asyncio.Runner") as mock_runner:

                def run_side_effect_eg(coro: Any) -> Any:
                    coro.close()
                    raise ExceptionGroup("group", [OSError("os error")])

                mock_runner.return_value.__enter__.return_value.run.side_effect = run_side_effect_eg
                with pytest.raises(SystemExit) as excinfo:
                    main()
                assert excinfo.value.code == 1

    # 4. uvloop is None
    with patch("mcubridge.daemon.load_runtime_config"):
        with patch("mcubridge.daemon.verify_crypto_integrity", return_value=True):
            with patch("mcubridge.daemon.uvloop", None):
                with pytest.raises(SystemExit) as excinfo:
                    main()
                assert excinfo.value.code == 1


@pytest.mark.asyncio
async def test_handshake_coverage_boost() -> None:
    from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing

    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/testport",
        serial_handshake_fatal_failures=2,
        serial_shared_secret=b"secret1234",
    )
    state = create_runtime_state(config)
    send_frame = AsyncMock(return_value=True)
    enqueue_mqtt = AsyncMock()
    acknowledge_frame = AsyncMock()

    manager = SerialHandshakeManager(
        config=config,
        state=state,
        serial_timing=derive_serial_timing(config),
        send_frame=send_frame,
        enqueue_mqtt=enqueue_mqtt,
        acknowledge_frame=acknowledge_frame,
    )

    # 1. handle_link_sync_resp without pending nonce
    state.link_handshake_nonce = None
    assert await manager.handle_link_sync_resp(1, b"") is False

    # 2. handle_link_sync_resp throttled due to rate limit
    state.link_handshake_nonce = b"\x00" * 16
    state.handshake_rate_until = 99999999.0
    with patch("time.monotonic", return_value=0.0):
        assert await manager.handle_link_sync_resp(1, b"") is False

    # 3. handle_link_sync_resp protobuf decode failure
    state.handshake_rate_until = 0.0
    assert await manager.handle_link_sync_resp(1, b"bad protobuf") is False

    # 4. handle_link_sync_resp mismatch tag
    state.link_handshake_nonce = b"\x00" * 16
    state.link_expected_tag = b"correct_tag"
    sync_resp_pb = pb.LinkSync(nonce=b"\x00" * 16, tag=b"wrong_tag_16_bytes")
    assert await manager.handle_link_sync_resp(1, sync_resp_pb.SerializeToString()) is False

    # 5. handle_handshake_failure fatal
    state.handshake_failure_streak = 5
    await manager.handle_handshake_failure("sync_auth_mismatch", detail="test_detail")
    assert state.handshake_fatal_count > 0
