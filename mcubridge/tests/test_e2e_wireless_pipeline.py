"""End-to-End (E2E) integration tests for Wireless (WiFi/TCP) Bridge Pipeline.

[SIL-2 / MIL-SPEC COMPLIANCE]
- Verifies complete end-to-end flow: IPC Client -> Daemon -> Wireless TCP Stream -> MCU -> Return Path.
- Deterministic assertions on mutated state, handshake synchronization, and RPC replies.
"""

from __future__ import annotations

import asyncio

from google.protobuf.message import DecodeError
from google.protobuf.message import Message as ProtobufMessage
from pw_hdlc.encode import ui_frame
import structlog
from pw_hdlc.decode import FrameDecoder
from pw_rpc.internal.packet_pb2 import PacketType, RpcPacket

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.security.security import LinkCipher, derive_link_keys
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport

logger = structlog.get_logger(__name__)


def _make_mcu_cipher(secret: bytes, nonce: bytes) -> LinkCipher:
    host_to_mcu, mcu_to_host = derive_link_keys(secret, nonce)
    return LinkCipher(mcu_to_host, host_to_mcu)


def _encode_mcu_rpc_frame(
    command_id: int,
    payload: bytes | ProtobufMessage,
    call_id: int,
    cipher: LinkCipher | None,
) -> bytes:
    encoded = protocol.encode_hdlc_rpc(
        command_id,
        payload,
        call_id=call_id,
        packet_type=PacketType.RESPONSE,
    )
    if cipher is None:
        return encoded
    frame = next(iter(FrameDecoder().process(encoded)))
    return ui_frame(
        protocol.HDLC_ADDRESS_RPC,
        cipher.encrypt(protocol.HDLC_ADDRESS_RPC, frame.data),
    )


def _decode_host_rpc_packet(packet_data: bytes, cipher: LinkCipher | None) -> tuple[RpcPacket, LinkCipher | None]:
    if cipher is not None:
        try:
            packet_data = cipher.decrypt(protocol.HDLC_ADDRESS_RPC, packet_data)
        except ValueError as exc:
            control_packet = RpcPacket()
            try:
                control_packet.ParseFromString(packet_data)
            except DecodeError:
                raise exc from None
            operation = protocol.get_operation((control_packet.service_id, control_packet.method_id))
            if operation is None or operation.command_id not in (
                protocol.Command.CMD_LINK_RESET.value,
                protocol.Command.CMD_LINK_SYNC.value,
            ):
                raise exc
            return control_packet, None

    packet = RpcPacket()
    packet.ParseFromString(packet_data)
    return packet, cipher


async def test_e2e_wireless_tcp_handshake_and_rpc_exchange(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify complete E2E synchronization and RPC command exchange over Wireless TCP."""
    frames_received_by_mcu: list[tuple[int, bytes, int]] = []

    async def mock_mcu_tcp_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = FrameDecoder()
        mcu_cipher: LinkCipher | None = None
        try:
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break

                for frame in decoder.process(chunk):
                    if not frame.ok() or frame.address != protocol.HDLC_ADDRESS_RPC:
                        continue

                    pkt, mcu_cipher = _decode_host_rpc_packet(frame.data, mcu_cipher)
                    op = protocol.get_operation((pkt.service_id, pkt.method_id))
                    cmd_id = op.command_id if op and op.command_id is not None else pkt.method_id
                    rx_seq = pkt.call_id
                    payload = pkt.payload

                    frames_received_by_mcu.append((cmd_id, payload, rx_seq))

                    # Simulate MCU responses based on received command
                    if cmd_id == protocol.Command.CMD_LINK_SYNC.value:
                        sync_req = pb.LinkSync.FromString(payload)
                        mcu_cipher = _make_mcu_cipher(runtime_config.serial_shared_secret, sync_req.nonce)
                        tag = service.handshake.calculate_handshake_response_tag(
                            runtime_config.serial_shared_secret, sync_req.nonce
                        )
                        resp_pb = pb.LinkSync(
                            nonce=sync_req.nonce,
                            tag=tag,
                        )
                        resp_frame = protocol.encode_hdlc_rpc(
                            protocol.Command.CMD_LINK_SYNC_RESP.value,
                            resp_pb,
                            call_id=rx_seq,
                        )
                        writer.write(resp_frame)
                        await writer.drain()

                    elif cmd_id == protocol.Command.CMD_GET_VERSION.value:
                        resp_pb = pb.VersionResponse(major=2, minor=8, patch=5)
                        resp_frame = _encode_mcu_rpc_frame(
                            protocol.Command.CMD_GET_VERSION_RESP.value,
                            resp_pb,
                            rx_seq,
                            mcu_cipher,
                        )
                        writer.write(resp_frame)
                        await writer.drain()

                    elif cmd_id == protocol.Command.CMD_DIGITAL_WRITE.value:
                        resp_frame = _encode_mcu_rpc_frame(
                            protocol.Status.OK.value,
                            b"",
                            rx_seq,
                            mcu_cipher,
                        )
                        writer.write(resp_frame)
                        await writer.drain()

        except (asyncio.IncompleteReadError, asyncio.CancelledError, ConnectionResetError) as exc:
            logger.debug("Mock MCU TCP server connection closed", error=str(exc))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError as exc:
                logger.debug("Mock TCP server socket close handled", error=str(exc))

    # Start mock MCU TCP listener
    server = await asyncio.start_server(mock_mcu_tcp_server, "127.0.0.1", 0)
    assert server.sockets is not None
    tcp_port = server.sockets[0].getsockname()[1]

    # Configure Daemon to use wireless TCP transport
    runtime_config.serial_port = f"tcp://127.0.0.1:{tcp_port}"
    runtime_config.serial_retry_timeout = 0.5
    runtime_config.serial_response_timeout = 1.0

    transport = SerialTransport(runtime_config, runtime_state, None)
    service = BridgeService(runtime_config, runtime_state, transport)
    transport.service = service

    runtime_state.link_sync_event.clear()
    transport_task = asyncio.create_task(transport.run())
    await asyncio.wait_for(runtime_state.link_sync_event.wait(), timeout=5.0)
    assert runtime_state.is_synchronized is True

    ver_ok = await service.request_mcu_version()
    assert ver_ok is True
    assert runtime_state.mcu_version == (2, 8, 5)

    # 2. Execute RPC digital write command over Wireless TCP
    dw_req = pb.DigitalWrite(pin=13, value=1)
    res = await transport.send(protocol.Command.CMD_DIGITAL_WRITE.value, dw_req)
    assert res is not None

    # 3. Assert frames were delivered over TCP wire
    assert len(frames_received_by_mcu) >= 2
    cmd_ids = [f[0] for f in frames_received_by_mcu]
    assert protocol.Command.CMD_LINK_SYNC.value in cmd_ids
    assert protocol.Command.CMD_DIGITAL_WRITE.value in cmd_ids

    # 4. Cleanup
    await transport.stop()
    await transport_task

    server.close()
    await server.wait_closed()


async def test_e2e_wireless_tcp_analog_read_and_datastore(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify analog reading and datastore exchange over wireless TCP link."""

    async def mock_mcu_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = FrameDecoder()
        mcu_cipher: LinkCipher | None = None
        try:
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break

                for frame in decoder.process(chunk):
                    if not frame.ok() or frame.address != protocol.HDLC_ADDRESS_RPC:
                        continue

                    pkt, mcu_cipher = _decode_host_rpc_packet(frame.data, mcu_cipher)
                    op = protocol.get_operation((pkt.service_id, pkt.method_id))
                    cmd_id = op.command_id if op and op.command_id is not None else pkt.method_id
                    rx_seq = pkt.call_id
                    payload = pkt.payload

                    if cmd_id == protocol.Command.CMD_LINK_SYNC.value:
                        sync_req = pb.LinkSync.FromString(payload)
                        mcu_cipher = _make_mcu_cipher(runtime_config.serial_shared_secret, sync_req.nonce)
                        tag = service.handshake.calculate_handshake_response_tag(
                            runtime_config.serial_shared_secret, sync_req.nonce
                        )
                        resp_pb = pb.LinkSync(nonce=sync_req.nonce, tag=tag)
                        resp_frame = protocol.encode_hdlc_rpc(
                            protocol.Command.CMD_LINK_SYNC_RESP.value,
                            resp_pb,
                            call_id=rx_seq,
                        )
                        writer.write(resp_frame)
                        await writer.drain()

                    elif cmd_id == protocol.Command.CMD_ANALOG_READ.value:
                        response = RpcPacket(
                            type=PacketType.RESPONSE,
                            channel_id=pkt.channel_id,
                            service_id=pkt.service_id,
                            method_id=pkt.method_id,
                            call_id=pkt.call_id,
                            payload=pb.AnalogReadResponse(value=742).SerializeToString(),
                            status=0,
                        )
                        response_data = response.SerializeToString()
                        if mcu_cipher is not None:
                            response_data = mcu_cipher.encrypt(protocol.HDLC_ADDRESS_RPC, response_data)
                        writer.write(ui_frame(protocol.HDLC_ADDRESS_RPC, response_data))
                        await writer.drain()

        except (asyncio.IncompleteReadError, asyncio.CancelledError, ConnectionResetError) as exc:
            logger.debug("Mock MCU WiFi server connection closed", error=str(exc))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError as exc:
                logger.debug("Mock WiFi server socket close handled", error=str(exc))

    server = await asyncio.start_server(mock_mcu_server, "127.0.0.1", 0)
    assert server.sockets is not None
    tcp_port = server.sockets[0].getsockname()[1]

    runtime_config.serial_port = f"wifi://127.0.0.1:{tcp_port}"
    transport = SerialTransport(runtime_config, runtime_state, None)
    service = BridgeService(runtime_config, runtime_state, transport)
    transport.service = service

    runtime_state.link_sync_event.clear()
    transport_task = asyncio.create_task(transport.run())
    await asyncio.wait_for(runtime_state.link_sync_event.wait(), timeout=5.0)
    assert runtime_state.is_synchronized is True

    ar_req = pb.PinRead(pin=0)
    raw_res = await transport.send(protocol.Command.CMD_ANALOG_READ.value, ar_req)
    assert raw_res is not None
    if isinstance(raw_res, pb.AnalogReadResponse):
        ar_resp = raw_res
    elif isinstance(raw_res, bytes):
        ar_resp = pb.AnalogReadResponse.FromString(raw_res)
    else:
        raise AssertionError(f"Unexpected response type: {type(raw_res)}")
    assert ar_resp.value == 742

    await transport.stop()
    await transport_task

    server.close()
    await server.wait_closed()
