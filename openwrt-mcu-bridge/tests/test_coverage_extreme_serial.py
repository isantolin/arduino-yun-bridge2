import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Mock serial_asyncio_fast
mock_saf = MagicMock()
mock_saf.create_serial_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
sys.modules["serial_asyncio_fast"] = mock_saf

import asyncio  # noqa: E402
import pytest  # noqa: E402
from mcubridge.transport import serial_fast  # noqa: E402
from mcubridge.rpc.protocol import Command, FRAME_DELIMITER  # noqa: E402
from mcubridge.rpc.frame import Frame  # noqa: E402
from cobs import cobs  # noqa: E402

def _make_config():
    config = MagicMock()
    config.serial_port = "/dev/ttyTest"
    config.serial_baud = 115200
    config.serial_safe_baud = 9600
    config.reconnect_delay = 0.01
    return config

@pytest.mark.asyncio
async def test_serial_binary_packet_all_paths():
    assert serial_fast._is_binary_packet(b"abc") is True
    assert serial_fast._is_binary_packet(b"") is False
    assert serial_fast._is_binary_packet(None) is False
    assert serial_fast._is_binary_packet(123) is False

@pytest.mark.asyncio
async def test_serial_protocol_connection_made_log():
    mock_service = AsyncMock()
    mock_state = MagicMock()
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, asyncio.get_running_loop())
    with patch.object(serial_fast.logger, "info") as mock_info:
        proto.connection_made(MagicMock())
        mock_info.assert_called_with("Serial transport established (Protocol).")

@pytest.mark.asyncio
async def test_serial_protocol_connection_lost_full():
    mock_service = AsyncMock()
    mock_state = MagicMock()
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, asyncio.get_running_loop())
    proto.connection_lost(None)
    proto._connected_future = asyncio.get_running_loop().create_future()
    proto.connection_lost(OSError("lost"))
    assert proto.transport is None

@pytest.mark.asyncio
async def test_serial_protocol_data_received_complex():
    mock_service = AsyncMock()
    mock_state = MagicMock()
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, asyncio.get_running_loop())
    proto.connection_made(MagicMock())
    huge = b"A" * (serial_fast.MAX_SERIAL_PACKET_BYTES + 1)
    proto.data_received(huge)
    assert proto._discarding is True
    proto.data_received(FRAME_DELIMITER)
    assert proto._discarding is False
    proto.data_received(b"partial")
    assert bytes(proto._buffer) == b"partial"

@pytest.mark.asyncio
async def test_serial_process_packet_negotiation_full():
    mock_service = AsyncMock()
    mock_state = MagicMock()
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, asyncio.get_running_loop())
    proto.negotiation_future = asyncio.get_running_loop().create_future()
    f = cobs.encode(Frame.build(Command.CMD_SET_BAUDRATE_RESP, b""))
    proto._process_packet(f)
    assert proto.negotiation_future.result() is True
    proto.negotiation_future = asyncio.get_running_loop().create_future()
    proto._process_packet(b"\x00")
    assert not proto.negotiation_future.done()

@pytest.mark.asyncio
async def test_serial_async_process_packet_full_coverage():
    mock_service = AsyncMock()
    mock_state = MagicMock()
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, asyncio.get_running_loop())
    await proto._async_process_packet(b"")
    with patch("mcubridge.transport.serial_fast.Frame.from_bytes", side_effect=ValueError("crc mismatch")):
        await proto._async_process_packet(cobs.encode(b"any"))
        assert mock_state.record_serial_crc_error.called

@pytest.mark.asyncio
async def test_serial_write_frame_full_coverage():
    mock_service = AsyncMock()
    mock_state = MagicMock()
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, asyncio.get_running_loop())
    proto.transport = MagicMock()
    proto.transport.is_closing.return_value = False
    with patch.object(serial_fast.logger, "isEnabledFor", return_value=True):
        proto.write_frame(Command.CMD_GET_VERSION, b"")
    proto.transport.write.side_effect = OSError("write fail")
    proto.write_frame(Command.CMD_GET_VERSION, b"")

@pytest.mark.asyncio
async def test_serial_transport_run_and_connect_failures():
    config = _make_config()
    transport = serial_fast.SerialTransport(config, MagicMock(), AsyncMock())
    with patch.object(transport, "_connect_and_run", side_effect=[RuntimeError("fail"), None]):
        async def stop_transport():
            await asyncio.sleep(0.05)
            transport._stop_event.set()
        asyncio.create_task(stop_transport())
        await transport.run()

@pytest.mark.asyncio
async def test_serial_transport_negotiation_failure_branch():
    config = _make_config()
    transport = serial_fast.SerialTransport(config, MagicMock(), AsyncMock())
    with patch.object(transport, "_negotiate_baudrate", return_value=False):
        m_t, m_p = MagicMock(), MagicMock()
        m_p.transport = m_t
        m_t.is_closing.side_effect = [False, True]
        with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection",
                   return_value=(m_t, m_p)):
            await transport._connect_and_run(asyncio.get_running_loop())

@pytest.mark.asyncio
async def test_serial_transport_disconnect_hook_coverage():
    config = _make_config()
    svc = AsyncMock()
    svc.on_serial_disconnected.side_effect = Exception("boom")
    transport = serial_fast.SerialTransport(config, MagicMock(), svc)
    m_t = MagicMock()
    m_t.is_closing.return_value = True
    with patch("mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection",
               return_value=(m_t, MagicMock())):
        await transport._connect_and_run(asyncio.get_running_loop())
