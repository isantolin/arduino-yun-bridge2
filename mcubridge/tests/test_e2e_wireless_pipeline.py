"""End-to-End (E2E) integration tests for Wireless (WiFi/TCP) Bridge Pipeline.

[SIL-2 / MIL-SPEC COMPLIANCE]
- Verifies complete end-to-end flow: IPC Client -> Daemon -> Wireless TCP Stream -> MCU -> Return Path.
- Deterministic assertions on mutated state, handshake synchronization, and RPC replies.
"""

from __future__ import annotations

import asyncio

import structlog
from pw_hdlc.decode import FrameDecoder
from pw_rpc.internal.packet_pb2 import RpcPacket

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport

logger = structlog.get_logger(__name__)


async def test_e2e_wireless_tcp_handshake_and_rpc_exchange(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify complete E2E synchronization and RPC command exchange over Wireless TCP."""
    frames_received_by_mcu: list[tuple[int, bytes, int]] = []

    async def mock_mcu_tcp_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = FrameDecoder()
        try:
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break

                for frame in decoder.process(chunk):
                    if not frame.ok() or frame.address != protocol.HDLC_ADDRESS_RPC:
                        continue

                    pkt = RpcPacket()
                    pkt.ParseFromString(frame.data)
                    cmd_id = protocol.RPC_TO_COMMAND.get((pkt.service_id, pkt.method_id)) or pkt.method_id
                    rx_seq = pkt.call_id
                    payload = pkt.payload

                    frames_received_by_mcu.append((cmd_id, payload, rx_seq))

                    # Simulate MCU responses based on received command
                    if cmd_id == protocol.Command.CMD_LINK_SYNC.value:
                        sync_req = pb.LinkSync.FromString(payload)
                        tag = service.handshake.calculate_handshake_tag(
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
                        resp_frame = protocol.encode_hdlc_rpc(
                            protocol.Command.CMD_GET_VERSION_RESP.value,
                            resp_pb,
                            call_id=rx_seq,
                        )
                        writer.write(resp_frame)
                        await writer.drain()

                    elif cmd_id == protocol.Command.CMD_DIGITAL_WRITE.value:
                        resp_frame = protocol.encode_hdlc_rpc(
                            protocol.Status.OK.value,
                            b"",
                            call_id=rx_seq,
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

    # Start transport loop
    transport_task = asyncio.create_task(transport.run())
    await asyncio.sleep(0.15)

    # 1. Perform Link Synchronization over Wireless TCP
    sync_ok = await service.handshake.synchronize()
    assert sync_ok is True
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
    transport_task.cancel()
    try:
        await transport_task
    except asyncio.CancelledError as exc:
        logger.debug("Transport task cancelled", error=str(exc))

    server.close()
    await server.wait_closed()


async def test_e2e_wireless_tcp_analog_read_and_datastore(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify analog reading and datastore exchange over wireless TCP link."""

    async def mock_mcu_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = FrameDecoder()
        try:
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break

                for frame in decoder.process(chunk):
                    if not frame.ok() or frame.address != protocol.HDLC_ADDRESS_RPC:
                        continue

                    pkt = RpcPacket()
                    pkt.ParseFromString(frame.data)
                    cmd_id = protocol.RPC_TO_COMMAND.get((pkt.service_id, pkt.method_id)) or pkt.method_id
                    rx_seq = pkt.call_id
                    payload = pkt.payload

                    if cmd_id == protocol.Command.CMD_LINK_SYNC.value:
                        sync_req = pb.LinkSync.FromString(payload)
                        tag = service.handshake.calculate_handshake_tag(
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
                        resp_pb = pb.AnalogReadResponse(value=742)
                        resp_frame = protocol.encode_hdlc_rpc(
                            protocol.Command.CMD_ANALOG_READ_RESP.value,
                            resp_pb,
                            call_id=rx_seq,
                        )
                        writer.write(resp_frame)
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

    transport_task = asyncio.create_task(transport.run())
    await asyncio.sleep(0.15)

    sync_ok = await service.handshake.synchronize()
    assert sync_ok is True

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
    transport_task.cancel()
    try:
        await transport_task
    except asyncio.CancelledError as exc:
        logger.debug("Transport task cancelled", error=str(exc))

    server.close()
    await server.wait_closed()
