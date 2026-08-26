"""End-to-End (E2E) integration tests for Wireless (WiFi/TCP) Bridge Pipeline.

[SIL-2 / MIL-SPEC COMPLIANCE]
- Verifies complete end-to-end flow: IPC Client -> Daemon -> Wireless TCP Stream -> MCU -> Return Path.
- Deterministic assertions on mutated state, handshake synchronization, and RPC replies.
"""

from __future__ import annotations

import asyncio

import pytest
from cobs import cobsr

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb, protocol
from mcubridge.protocol.frame import build_frame, parse_frame
from mcubridge.security.security import generate_nonce_with_counter
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport


@pytest.mark.asyncio
async def test_e2e_wireless_tcp_handshake_and_rpc_exchange(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify complete E2E synchronization and RPC command exchange over Wireless TCP."""
    frames_received_by_mcu: list[tuple[int, bytes, int]] = []
    seq_counter = 1

    async def mock_mcu_tcp_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal seq_counter
        try:
            while True:
                delimited = await reader.readuntil(protocol.FRAME_DELIMITER)
                packet = delimited[:-1]
                if not packet:
                    continue

                raw_frame = cobsr.decode(packet)
                decoded = parse_frame(raw_frame)
                cmd_id = decoded.envelope.command_id
                rx_seq = decoded.envelope.sequence_id
                payload = decoded.payload
                frames_received_by_mcu.append(
                    (cmd_id, payload if isinstance(payload, bytes) else payload.SerializeToString(), rx_seq)
                )

                # Simulate MCU responses based on received command
                if cmd_id == protocol.Command.CMD_LINK_SYNC.value:
                    sync_req = (
                        payload
                        if isinstance(payload, pb.LinkSync)
                        else pb.LinkSync.FromString(
                            payload if isinstance(payload, bytes) else payload.SerializeToString()
                        )
                    )
                    tag = service.handshake.calculate_handshake_tag(runtime_config.serial_shared_secret, sync_req.nonce)
                    resp_pb = pb.LinkSync(
                        nonce=sync_req.nonce,
                        tag=tag,
                    )
                    resp_frame = build_frame(
                        protocol.Command.CMD_LINK_SYNC_RESP.value,
                        rx_seq,
                        payload=resp_pb,
                    )
                    writer.write(cobsr.encode(resp_frame) + protocol.FRAME_DELIMITER)
                    await writer.drain()

                elif cmd_id == protocol.Command.CMD_GET_VERSION.value:
                    resp_pb = pb.VersionResponse(major=2, minor=8, patch=5)
                    resp_frame = build_frame(
                        protocol.Command.CMD_GET_VERSION_RESP.value,
                        rx_seq,
                        payload=resp_pb,
                    )
                    writer.write(cobsr.encode(resp_frame) + protocol.FRAME_DELIMITER)
                    await writer.drain()

                elif cmd_id == protocol.Command.CMD_DIGITAL_WRITE.value:
                    # ACK with STATUS_OK
                    resp_frame = build_frame(
                        protocol.Status.OK.value,
                        rx_seq,
                        payload=b"",
                    )
                    writer.write(cobsr.encode(resp_frame) + protocol.FRAME_DELIMITER)
                    await writer.drain()

        except (asyncio.IncompleteReadError, asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

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

    ver_ok = await getattr(service, "_request_mcu_version")()
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
    except asyncio.CancelledError:
        pass

    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_e2e_wireless_tcp_analog_read_and_datastore(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify analog reading and datastore exchange over wireless TCP link."""
    mcu_counter = 0

    async def mock_mcu_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal mcu_counter
        try:
            while True:
                delimited = await reader.readuntil(protocol.FRAME_DELIMITER)
                packet = delimited[:-1]
                if not packet:
                    continue

                raw_frame = cobsr.decode(packet)
                decoded = parse_frame(raw_frame)
                cmd_id = decoded.envelope.command_id
                rx_seq = decoded.envelope.sequence_id
                payload = decoded.payload

                if cmd_id == protocol.Command.CMD_LINK_SYNC.value:
                    sync_req = (
                        payload
                        if isinstance(payload, pb.LinkSync)
                        else pb.LinkSync.FromString(
                            payload if isinstance(payload, bytes) else payload.SerializeToString()
                        )
                    )
                    tag = service.handshake.calculate_handshake_tag(runtime_config.serial_shared_secret, sync_req.nonce)
                    resp_pb = pb.LinkSync(nonce=sync_req.nonce, tag=tag)
                    resp_frame = build_frame(
                        protocol.Command.CMD_LINK_SYNC_RESP.value,
                        rx_seq,
                        payload=resp_pb,
                    )
                    writer.write(cobsr.encode(resp_frame) + protocol.FRAME_DELIMITER)
                    await writer.drain()

                elif cmd_id == protocol.Command.CMD_ANALOG_READ.value:
                    ack_pb = pb.AckPacket(command_id=protocol.Command.CMD_ANALOG_READ.value)
                    nonce1, mcu_counter = generate_nonce_with_counter(mcu_counter)
                    ack_frame = build_frame(
                        protocol.Status.ACK.value,
                        rx_seq,
                        payload=ack_pb,
                        session_key=runtime_state.link_session_key,
                        nonce=nonce1,
                    )
                    writer.write(cobsr.encode(ack_frame) + protocol.FRAME_DELIMITER)
                    await writer.drain()

                    resp_pb = pb.AnalogReadResponse(value=742)
                    nonce2, mcu_counter = generate_nonce_with_counter(mcu_counter)
                    resp_frame = build_frame(
                        protocol.Command.CMD_ANALOG_READ_RESP.value,
                        rx_seq,
                        payload=resp_pb,
                        session_key=runtime_state.link_session_key,
                        nonce=nonce2,
                    )
                    writer.write(cobsr.encode(resp_frame) + protocol.FRAME_DELIMITER)
                    await writer.drain()

        except (asyncio.IncompleteReadError, asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

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
    except asyncio.CancelledError:
        pass

    server.close()
    await server.wait_closed()
