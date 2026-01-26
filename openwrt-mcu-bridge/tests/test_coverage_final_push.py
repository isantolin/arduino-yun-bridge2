import sys
import struct
import asyncio
import logging
import pytest
from binascii import crc32
from unittest.mock import MagicMock, AsyncMock, patch

# 1. Mock serial_asyncio_fast globally
mock_saf = MagicMock()
mock_saf.create_serial_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
sys.modules["serial_asyncio_fast"] = mock_saf

from mcubridge.transport import serial_fast  # noqa: E402
from mcubridge.rpc import protocol  # noqa: E402
from mcubridge.rpc.frame import Frame  # noqa: E402
from mcubridge.rpc.protocol import Command, FRAME_DELIMITER  # noqa: E402
from cobs import cobs  # noqa: E402

# --- PROTOCOL: frame.py 100% ---

def _build_raw_with_crc(data_no_crc: bytes) -> bytes:
    c = crc32(data_no_crc) & protocol.CRC32_MASK
    return data_no_crc + struct.pack(protocol.CRC_FORMAT, c)

def test_frame_parse_coverage_all_errors():
    # Line 112: Incomplete header (Passes MIN_FRAME_SIZE but fails structural check)
    with patch("mcubridge.rpc.protocol.MIN_FRAME_SIZE", 5):
        with pytest.raises(ValueError, match="Incomplete header"):
            Frame.parse(b"12345")

    # Line 125: CRC Mismatch
    bad_crc_frame = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 0, 0x40) + b"\x00\x00\x00\x00"
    with pytest.raises(ValueError, match="CRC mismatch"):
        Frame.parse(bad_crc_frame)

    # Line 140: Invalid version
    bad_ver = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, 255, 0, 0x40)
    with pytest.raises(ValueError, match="Invalid version"):
        Frame.parse(_build_raw_with_crc(bad_ver))

    # Line 149: Invalid command id (reserved)
    bad_cmd = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 0, 0)
    with pytest.raises(ValueError, match="Invalid command id"):
        Frame.parse(_build_raw_with_crc(bad_cmd))

    # Line 157: Payload length mismatch
    bad_len = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 10, 0x40)
    raw = _build_raw_with_crc(bad_len)
    with pytest.raises(ValueError, match="Payload length mismatch"):
        Frame.parse(raw)

def test_frame_build_edge_cases():
    with pytest.raises(ValueError, match="outside 16-bit range"):
        Frame.build(-1)
    with pytest.raises(ValueError, match="outside 16-bit range"):
        Frame.build(70000)
    with pytest.raises(ValueError, match="Payload too large"):
        Frame.build(0x40, b"A" * 1000)

# --- SERIAL: serial_fast.py 100% ---

@pytest.mark.asyncio
async def test_serial_protocol_extreme_coverage():
    mock_service = AsyncMock()
    mock_state = MagicMock()
    loop = asyncio.get_running_loop()
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, loop)
    
    # 1. Connection made future logic (Lines 64-65)
    mock_transport = MagicMock()
    proto.connection_made(mock_transport)
    assert proto.transport == mock_transport
    assert proto._connected_future.done()
    
    # 2. Connection lost futures and exc (Lines 95-98)
    proto._connected_future = loop.create_future()
    proto.connection_lost(OSError("physical disconnect"))
    assert proto.transport is None
    assert proto._connected_future.exception() is not None
    
    # 3. data_received slow path, accumulation, discarding (Lines 108-128)
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, loop)
    proto.connection_made(mock_transport)
    # Fragmented packet
    proto.data_received(b"\x01\x02")
    assert bytes(proto._buffer) == b"\x01\x02"
    # Overlimit packet triggers discarding
    huge = b"A" * (serial_fast.MAX_SERIAL_PACKET_BYTES + 1)
    proto.data_received(huge)
    assert proto._discarding is True
    # Delimiter clears discarding
    proto.data_received(FRAME_DELIMITER)
    assert proto._discarding is False
    assert len(proto._buffer) == 0
    # Remnant in fast path (Line 151-158)
    proto.data_received(b"fast_partial")
    assert bytes(proto._buffer) == b"fast_partial"
    
    # 4. _process_packet negotiation branches (Lines 170-176)
    proto.negotiation_future = loop.create_future()
    # DecodeError path
    with patch("mcubridge.transport.serial_fast.cobs.decode", side_effect=Exception("decode fail")):
        proto._process_packet(b"junk")
        assert not proto.negotiation_future.done()
    # Success path
    f = cobs.encode(Frame.build(Command.CMD_SET_BAUDRATE_RESP, b""))
    proto._process_packet(f)
    assert proto.negotiation_future.result() is True

    # 5. _async_process_packet binary check fail (Lines 187-188)
    await proto._async_process_packet(b"")
    
    # 6. _async_process_packet CRC/Decode exceptions (Lines 203-212)
    # Zero byte in input exception
    await proto._async_process_packet(b"\x00")
    # Custom exception
    with patch("mcubridge.transport.serial_fast.cobs.decode", side_effect=ValueError("crc mismatch")):
        await proto._async_process_packet(b"abc")
        assert mock_state.record_serial_crc_error.called

    # 7. write_frame no-payload path & exception (Lines 245-246, 254)
    proto.transport = MagicMock()
    proto.transport.is_closing.return_value = False
    with patch.object(serial_fast.logger, "isEnabledFor", return_value=True):
        proto.write_frame(Command.CMD_GET_VERSION, b"")
    # OSError path
    proto.transport.write.side_effect = OSError("write fail")
    assert proto.write_frame(Command.CMD_GET_VERSION, b"") is False

@pytest.mark.asyncio
async def test_serial_transport_lifecycle_coverage():
    config = MagicMock()
    config.reconnect_delay = 0
    config.serial_baud = 115200
    config.serial_safe_baud = 9600
    state = MagicMock()
    service = AsyncMock()
    transport = serial_fast.SerialTransport(config, state, service)
    
    # Line 257-259 (init)
    assert transport.service == service
    
    # Line 281-289 (run errors & stop)
    with patch.object(transport, "_connect_and_run", side_effect=[RuntimeError("fail"), None]):
        async def stop_transport():
            await asyncio.sleep(0.05)
            transport._stop_event.set()
        asyncio.create_task(stop_transport())
        await transport.run()

    # Line 315 (negotiation failure branch)
    with patch.object(transport, "_negotiate_baudrate", return_value=False):
        m_t, m_p = MagicMock(), MagicMock()
        m_p.transport = m_t
        m_t.is_closing.side_effect = [False, True]
        with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", return_value=(m_t, m_p)):
            await transport._connect_and_run(asyncio.get_running_loop())

    # Line 325 (disconnect hook exception)
    service.on_serial_disconnected.side_effect = Exception("hook error")
    transport.service = service
    m_t, m_p = MagicMock(), MagicMock()
    m_t.is_closing.return_value = True
    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection", return_value=(m_t, m_p)):
        await transport._connect_and_run(asyncio.get_running_loop())