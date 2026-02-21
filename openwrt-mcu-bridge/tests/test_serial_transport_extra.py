"""Extra coverage for mcubridge.transport.serial."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame
from mcubridge.transport.serial import (
    BridgeSerialProtocol,
    SerialTransport,
    serial_sender_not_ready,
)


@pytest.mark.asyncio
async def test_serial_sender_not_ready_logs() -> None:
    # Just call it to cover line 52
    await serial_sender_not_ready(0x40, b"")


@pytest.mark.asyncio
async def test_serial_protocol_process_packet_baud_resp() -> None:
    ctx = MagicMock()
    state = MagicMock()
    loop = asyncio.get_running_loop()
    proto = BridgeSerialProtocol(ctx, state, loop)
    proto.negotiation_future = loop.create_future()

    # Valid CMD_SET_BAUDRATE_RESP
    from cobs import cobs
    raw_frame = Frame.build(protocol.Command.CMD_SET_BAUDRATE_RESP.value, b"")
    encoded = cobs.encode(raw_frame)

    proto._process_packet(encoded)
    assert proto.negotiation_future.done()
    assert await proto.negotiation_future is True


@pytest.mark.asyncio
async def test_serial_protocol_async_process_compressed() -> None:
    service = MagicMock()
    service.handle_mcu_frame = AsyncMock()
    state = MagicMock()
    loop = asyncio.get_running_loop()
    proto = BridgeSerialProtocol(service, state, loop)

    # Compressed frame
    from mcubridge.protocol import rle
    payload = b"AAAAABBB"
    compressed = rle.encode(payload)
    cmd = protocol.Command.CMD_CONSOLE_WRITE.value | protocol.CMD_FLAG_COMPRESSED

    from cobs import cobs
    raw_frame = Frame.build(cmd, compressed)
    encoded = cobs.encode(raw_frame)

    await proto._async_process_packet(encoded)
    service.handle_mcu_frame.assert_called()
    # Payload should be decompressed
    assert service.handle_mcu_frame.call_args[0][1] == payload


    @pytest.mark.asyncio
    async def test_serial_transport_toggle_dtr_fail() -> None:
        config = MagicMock()
        state = MagicMock()
        service = MagicMock()
        transport = SerialTransport(config, state, service)

        with patch.object(transport, "_blocking_reset", side_effect=RuntimeError("dtr fail")):
            # Should log and continue
            await transport._toggle_dtr(asyncio.get_running_loop())

@pytest.mark.asyncio
async def test_serial_transport_negotiate_fail_paths() -> None:
    config = MagicMock()
    state = MagicMock()
    service = MagicMock()
    transport = SerialTransport(config, state, service)

    proto = MagicMock()
    proto.write_frame.return_value = False # Write fail

    res = await transport._negotiate_baudrate(proto, 115200)
    assert res is False

    proto.write_frame.return_value = True
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        res = await transport._negotiate_baudrate(proto, 115200)
        assert res is False
