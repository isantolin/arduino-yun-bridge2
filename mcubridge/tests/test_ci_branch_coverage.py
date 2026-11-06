"""Targeted regression tests for uncovered runtime control-flow branches."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pw_hdlc.decode import FrameDecoder
from pw_hdlc.encode import ui_frame
from pytest_mock import MockerFixture

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.security.security import LinkCipher, verify_crypto_integrity
from mcubridge.services.handshake import (
    HandshakeEvent,
    HandshakeState,
    SerialHandshakeManager,
)
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport


async def test_publish_cloud_message_debug_logging_branch(
    mock_bridge_service: BridgeService,
    mocker: MockerFixture,
) -> None:
    service = mock_bridge_service
    stream = MagicMock()
    stream.send_message = AsyncMock()
    service.cloud_stream = stream
    mocker.patch("mcubridge.services.runtime.logger.is_enabled_for", return_value=True)

    message = pb.CloudQueuedPublish(topic_name="bridge/status", payload=b"online")
    assert await service.publish_cloud_message(message)
    stream.send_message.assert_awaited_once()


def test_cleanup_without_serial_transport(mock_bridge_service: BridgeService) -> None:
    mock_bridge_service.serial = None

    mock_bridge_service.cleanup()

    assert mock_bridge_service.serial is None


async def test_on_serial_connected_rejects_unsynchronized_link(
    mock_bridge_service: BridgeService,
    mocker: MockerFixture,
) -> None:
    mocker.patch.object(mock_bridge_service.handshake, "synchronize", new=AsyncMock(return_value=False))

    with pytest.raises(ConnectionError, match="handshake synchronization failed"):
        await mock_bridge_service.on_serial_connected()


async def test_file_read_unsafe_path_without_serial_returns_empty(
    mock_bridge_service: BridgeService,
) -> None:
    mock_bridge_service.serial = None

    response = await mock_bridge_service.file_read(1, pb.FileRead(path="../../../etc/passwd"))

    assert response.content == b""


async def test_console_write_empty_payload_is_noop(mock_bridge_service: BridgeService) -> None:
    response = await mock_bridge_service.console_write(pb.ConsoleWrite(data=b""))

    assert response.status == "ok"
    assert len(mock_bridge_service.state.console_to_mcu_queue) == 0


async def test_console_write_from_mcu_empty_payload_is_noop(mock_bridge_service: BridgeService) -> None:
    response = await mock_bridge_service.console_write(0, pb.ConsoleWrite(data=b""))

    assert response.status == "ok"


async def test_reset_link_without_serial_returns_false(mock_bridge_service: BridgeService) -> None:
    mock_bridge_service.serial = None

    assert not await mock_bridge_service.reset_link()


async def test_send_cloud_event_without_cloud_stream_is_noop(
    mock_bridge_service: BridgeService,
    mocker: MockerFixture,
) -> None:
    mock_bridge_service.cloud_stream = None
    debug_log = mocker.patch("mcubridge.services.runtime.logger.debug")
    mocker.patch("mcubridge.services.runtime.logger.is_enabled_for", return_value=True)

    await mock_bridge_service.send_cloud_event("status_online", "info", "Device online")

    debug_log.assert_not_called()


async def test_send_cloud_event_debug_logging_branch(
    mock_bridge_service: BridgeService,
    mocker: MockerFixture,
) -> None:
    stream = MagicMock()
    stream.send_message = AsyncMock()
    mock_bridge_service.cloud_stream = stream
    debug_log = mocker.patch("mcubridge.services.runtime.logger.debug")
    mocker.patch("mcubridge.services.runtime.logger.is_enabled_for", return_value=True)

    await mock_bridge_service.send_cloud_event("status_online", "info", "Device online")

    debug_log.assert_called_once()
    stream.send_message.assert_awaited_once()


async def test_handle_mcu_frame_without_serial_is_noop(
    mock_bridge_service: BridgeService,
) -> None:
    mock_bridge_service.serial = None

    await mock_bridge_service.handle_mcu_frame(protocol.Command.CMD_XOFF.value, 1, b"")

    assert not mock_bridge_service.state.mcu_is_paused


async def test_handle_mcu_frame_tuple_falls_back_to_command_id_alias(
    mock_bridge_service: BridgeService,
) -> None:
    service = mock_bridge_service
    service.state.state = "synchronized"
    handler = AsyncMock()
    service.mcu_registry.clear()
    service.mcu_registry[protocol.Command.CMD_XOFF.value] = handler

    await service.handle_mcu_frame((0x1234, protocol.Command.CMD_XOFF.value), 9, b"")

    handler.assert_awaited_once_with(9, b"")


async def test_on_serial_disconnected_without_serial_transport(
    mock_bridge_service: BridgeService,
) -> None:
    mock_bridge_service.serial = None

    await mock_bridge_service.on_serial_disconnected()

    assert not mock_bridge_service.state.mcu_is_paused


async def test_flush_console_queue_without_serial_is_noop(
    mock_bridge_service: BridgeService,
) -> None:
    mock_bridge_service.serial = None
    mock_bridge_service.state.console_to_mcu_queue.append(b"pending")

    await mock_bridge_service.flush_console_queue()

    assert len(mock_bridge_service.state.console_to_mcu_queue) == 1


def test_link_cipher_counter_exhaustion_and_zero_counter(mocker: MockerFixture) -> None:
    key = b"\x11" * protocol.AEAD_KEY_SIZE
    cipher = LinkCipher(key, key)
    object.__setattr__(cipher, "_tx_counter", 0xFFFFFFFFFFFFFFFF)
    with pytest.raises(OverflowError, match="transmit counter exhausted"):
        cipher.encrypt(protocol.HDLC_ADDRESS_RPC, b"payload")

    zero_counter_envelope = b"\x00" * (protocol.AEAD_COUNTER_SIZE + protocol.AEAD_TAG_SIZE)
    with pytest.raises(ValueError, match="counter is invalid or replayed"):
        cipher.decrypt(protocol.HDLC_ADDRESS_RPC, zero_counter_envelope)

    with pytest.raises(ValueError, match="address is out of range"):
        cipher.decrypt(256, zero_counter_envelope)

    mocker.patch(
        "cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305.decrypt",
        return_value=b"wrong",
    )
    assert not verify_crypto_integrity()


async def test_handshake_session_key_and_transition_failure_branches(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
    mocker: MockerFixture,
) -> None:
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    runtime_config.serial_shared_secret = b"shared-secret-key"
    runtime_config.serial_handshake_min_interval = 0.0

    hs = SerialHandshakeManager(
        config=runtime_config,
        state=runtime_state,
        serial_timing=pb.HandshakeConfig(response_timeout_ms=0),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
    )
    tag = hs.calculate_handshake_response_tag(runtime_config.serial_shared_secret, nonce)
    pkt = pb.LinkSync(nonce=nonce, tag=tag)

    # 1. LinkCipher.for_host raises ValueError
    runtime_state.link_handshake_nonce = nonce
    runtime_state.link_expected_tag = tag
    mocker.patch.object(LinkCipher, "for_host", side_effect=ValueError("bad key"))
    assert not await hs.handle_link_sync_resp(1, pkt)
    assert runtime_state.last_handshake_error == "session_key_derivation_failed"

    # 2. FSM not in SYNCING/CONFIRMING when SYNC_CONFIRMED fires -> transition rejected
    mocker.stopall()
    hs.transition(HandshakeEvent.RESET)
    assert hs.fsm_state == HandshakeState.UNSYNCHRONIZED
    runtime_state.link_handshake_nonce = nonce
    runtime_state.link_expected_tag = tag
    assert not await hs.handle_link_sync_resp(2, pkt)
    assert runtime_state.last_handshake_error == "sync_state_transition_failed"

    # 3. wait_for_link_sync_confirmation when response_timeout_ms == 0 and retry_timeout == 0
    runtime_config.serial_retry_timeout = 0.0
    runtime_state.connection_fsm.synchronize()
    assert await hs.wait_for_link_sync_confirmation(nonce)


async def test_serial_transport_aead_and_pre_handshake_branches(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    runtime_config.serial_shared_secret = b"transport-secret"
    service = MagicMock()
    service.handle_mcu_frame = AsyncMock()
    service.mcu_registry = {
        protocol.Command.CMD_LINK_SYNC_RESP.value: AsyncMock(),
    }
    transport = SerialTransport(runtime_config, runtime_state, service)
    mock_serial = MagicMock()
    mock_serial.is_open = True
    mock_serial.write = MagicMock(return_value=16)
    mock_serial.flush = MagicMock()
    transport.serial = mock_serial
    runtime_state.serial_tx_allowed.set()

    # 1. Pre-handshake (link_cipher is None): non-RPC frame is dropped without incrementing decode errors
    console_frame = next(iter(FrameDecoder().process(protocol.encode_hdlc_console(b"early"))))
    await transport.process_hdlc_frame(console_frame)
    assert runtime_state.serial_decode_errors == 0

    # 2. Pre-handshake: non-LinkSync RPC frame is rejected and increments decode errors
    ver_raw = protocol.encode_hdlc_rpc(protocol.Command.CMD_GET_VERSION.value, b"", call_id=1)
    ver_frame = next(iter(FrameDecoder().process(ver_raw)))
    await transport.process_hdlc_frame(ver_frame)
    assert runtime_state.serial_decode_errors == 1

    # 3. Pre-handshake: LinkSync response frame is accepted
    sync_resp_raw = protocol.encode_hdlc_rpc(
        protocol.Command.CMD_LINK_SYNC_RESP.value,
        pb.LinkSync(
            nonce=b"\x01" * protocol.HANDSHAKE_NONCE_LENGTH,
            tag=b"\x02" * protocol.HANDSHAKE_TAG_LENGTH,
        ),
        call_id=2,
    )
    sync_resp_frame = next(iter(FrameDecoder().process(sync_resp_raw)))
    await transport.process_hdlc_frame(sync_resp_frame)
    assert runtime_state.serial_decode_errors == 1

    # 4. Pre-handshake send_raw guards when secret is configured and cipher is None
    assert not await transport.send_raw(protocol.Command.CMD_CONSOLE_WRITE.value, b"console")
    assert not await transport.send_raw(protocol.Command.CMD_GET_VERSION.value, b"")
    assert await transport.send_raw(protocol.Command.CMD_LINK_RESET.value, b"")

    # 5. Post-handshake (cipher installed): invalid encrypted frame is rejected
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    runtime_state.link_cipher = LinkCipher.for_host(runtime_config.serial_shared_secret, nonce)
    bad_encrypted_frame = next(iter(FrameDecoder().process(ui_frame(protocol.HDLC_ADDRESS_RPC, b"\x00" * 4))))
    await transport.process_hdlc_frame(bad_encrypted_frame)
    assert runtime_state.serial_decode_errors == 2

    # 6. Post-handshake send_raw guards: CMD_LINK_SYNC rejected while cipher active; CMD_LINK_RESET sent cleartext
    assert not await transport.send_raw(protocol.Command.CMD_LINK_SYNC.value, b"")
    assert await transport.send_raw(protocol.Command.CMD_LINK_RESET.value, b"")
    assert await transport.send_raw(
        protocol.Command.CMD_CONSOLE_WRITE.value,
        pb.ConsoleWrite(data=b"encrypted-console"),
    )

    # 7. Cleartext console write when shared secret is empty and payload has no .data
    runtime_config.serial_shared_secret = b""
    runtime_state.link_cipher = None
    assert await transport.send_raw(protocol.Command.CMD_CONSOLE_WRITE.value, pb.Empty())
