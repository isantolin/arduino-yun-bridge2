import sys
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

# Mock serial_asyncio_fast at the very beginning
mock_saf = MagicMock()
mock_saf.create_serial_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
sys.modules["serial_asyncio_fast"] = mock_saf

from mcubridge.transport import serial_fast  # noqa: E402
from mcubridge.rpc.protocol import Command, FRAME_DELIMITER  # noqa: E402
from mcubridge.rpc.frame import Frame  # noqa: E402
from cobs import cobs  # noqa: E402


def _make_config():
    config = MagicMock()
    config.serial_port = "/dev/ttyTest"
    config.serial_baud = 115200
    config.serial_safe_baud = 115200
    config.reconnect_delay = 0.01
    return config


class RealAsyncTransport(asyncio.Transport):
    def __init__(self):
        super().__init__()
        self._closing = False
        self.written_data = []

    def is_closing(self):
        return self._closing

    def close(self):
        self._closing = True

    def write(self, data):
        if data == b"FAIL":
            raise OSError("hard fail")
        self.written_data.append(data)


@pytest.mark.asyncio
async def test_serial_protocol_100_percent_coverage():
    mock_service = AsyncMock()
    mock_state = MagicMock()
    loop = asyncio.get_running_loop()
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, loop)

    # 1. connection_made & connection_lost coverage
    transport = RealAsyncTransport()
    proto.connection_made(transport)
    assert proto.transport == transport

    # Already done future branch (Line 65)
    proto.connection_made(transport)

    # connection_lost logic (Lines 91-98)
    proto.connection_lost(None)
    assert proto.transport is None

    # Connection lost with exception
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, loop)
    proto.connection_lost(RuntimeError("wire cut"))

    # 2. data_received slow path & discarding (Lines 108-128)
    proto = serial_fast.BridgeSerialProtocol(mock_service, mock_state, loop)
    proto.connection_made(transport)

    # Slow path: accumulation (Line 121)
    proto.data_received(b"A")
    assert bytes(proto._buffer) == b"A"

    # Discarding state (Lines 115-118, 125-128)
    proto.data_received(b"B" * (serial_fast.MAX_SERIAL_PACKET_BYTES + 10))
    assert proto._discarding is True
    proto.data_received(FRAME_DELIMITER)
    assert proto._discarding is False

    # 3. _process_chunk_fast remnants (Lines 151-158)
    proto.data_received(b"remnant")
    assert bytes(proto._buffer) == b"remnant"

    # 4. Negotiation branches (Lines 170-176)
    proto.negotiation_future = loop.create_future()
    # Malformed COBS during negotiation
    proto._process_packet(b"\x00")
    assert not proto.negotiation_future.done()

    # Successful negotiation
    valid_resp = cobs.encode(Frame.build(Command.CMD_SET_BAUDRATE_RESP, b""))
    proto._process_packet(valid_resp)
    assert proto.negotiation_future.result() is True

    # 5. _async_process_packet error handling (Lines 187-188, 203-212)
    # Binary check fail
    await proto._async_process_packet(b"")

    # Frame parse error & CRC Mismatch (Line 211)
    with patch(
        "mcubridge.transport.serial_fast.Frame.from_bytes",
        side_effect=ValueError("it is a crc mismatch error"),
    ):
        await proto._async_process_packet(cobs.encode(b"any"))
        assert mock_state.record_serial_crc_error.called

    # 6. write_frame edge cases (Lines 245-246, 254)
    proto.transport = transport
    transport._closing = False
    # No payload debug log
    with patch.object(serial_fast.logger, "isEnabledFor", return_value=True):
        proto.write_frame(Command.CMD_GET_VERSION, b"")

    # Write exception
    transport.write = MagicMock(side_effect=OSError("physical error"))
    assert proto.write_frame(Command.CMD_GET_VERSION, b"") is False


@pytest.mark.asyncio
async def test_serial_transport_lifecycle_100_percent():
    config = _make_config()
    mock_state = MagicMock()
    mock_service = AsyncMock()
    transport_mgr = serial_fast.SerialTransport(config, mock_state, mock_service)

    # 1. run() exception & retry (Lines 281-289)
    # Force _connect_and_run to fail with OSError
    with patch.object(transport_mgr, "_connect_and_run", side_effect=[OSError("tty busy"), None]):

        async def stop_soon():
            await asyncio.sleep(0.05)
            transport_mgr._stop_event.set()

        asyncio.create_task(stop_soon())
        await transport_mgr.run()

    # 2. _connect_and_run hook exception (Line 315)
    mock_service.on_serial_disconnected.side_effect = RuntimeError("hook crash")
    m_t, m_p = MagicMock(), MagicMock()
    m_t.is_closing.return_value = True
    with patch(
        "mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection",
        return_value=(m_t, m_p),
    ):
        await transport_mgr._connect_and_run(asyncio.get_running_loop())


@pytest.mark.asyncio
async def test_serial_transport_negotiation_fail_coverage():
    config = _make_config()
    # Force negotiation needed
    config.serial_safe_baud = 9600
    config.serial_baud = 115200
    transport_mgr = serial_fast.SerialTransport(config, MagicMock(), AsyncMock())

    # Negotiation fail branch (Line 315)
    with patch.object(transport_mgr, "_negotiate_baudrate", return_value=False):
        m_t, m_p = RealAsyncTransport(), MagicMock()
        m_p.transport = m_t
        # Make transport closing after one loop to exit
        m_t._closing = True
        with patch(
            "mcubridge.transport.serial_fast.serial_asyncio_fast.create_serial_connection",
            return_value=(m_t, m_p),
        ):
            await transport_mgr._connect_and_run(asyncio.get_running_loop())
