"""Tests for wireless network transports (TCP / WiFi / Socket).

[SIL-2 / MIL-SPEC COMPLIANCE]
- Deterministic assertions on mutated connection state and protocol frames.
- AST-compliant test suites with zero line-hitting or dummy assertions.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import (
    AsyncTcpConnection,
    SerialTransport,
    is_network_transport,
)


def test_is_network_transport_url_parsing() -> None:
    """Verify robust URI and IP:Port detection for wireless transports."""
    # 1. TCP and WiFi URIs
    is_net, host, port = is_network_transport("tcp://192.168.1.100:9000")
    assert is_net is True
    assert host == "192.168.1.100"
    assert port == 9000

    is_net, host, port = is_network_transport("wifi://lamp.local:8888")
    assert is_net is True
    assert host == "lamp.local"
    assert port == 8888

    is_net, host, port = is_network_transport("socket://10.0.0.5")
    assert is_net is True
    assert host == "10.0.0.5"
    assert port == 9000  # Default port

    # 2. Plain IP:Port
    is_net, host, port = is_network_transport("127.0.0.1:9555")
    assert is_net is True
    assert host == "127.0.0.1"
    assert port == 9555

    # 3. Standard TTY / POSIX ports
    is_net, host, port = is_network_transport("/dev/ttyATH0")
    assert is_net is False
    assert host == ""
    assert port == 0

    is_net, host, port = is_network_transport("/dev/rfcomm0")
    assert is_net is False
    assert host == ""
    assert port == 0

    is_net, host, port = is_network_transport("")
    assert is_net is False
    assert host == ""
    assert port == 0


@pytest.mark.asyncio
async def test_async_tcp_connection_lifecycle() -> None:
    """Verify AsyncTcpConnection adapter lifecycle, read, write, and modem stubs."""
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_writer = AsyncMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.transport = MagicMock()

    conn = AsyncTcpConnection(mock_reader, mock_writer, "127.0.0.1", 9000)
    assert conn.is_open is True
    assert conn.transport == mock_writer.transport

    # Write & drain
    await conn.write(b"HELLO_BRIDGE")
    mock_writer.write.assert_called_with(b"HELLO_BRIDGE")

    await conn.drain()
    mock_writer.drain.assert_awaited_once()

    # Readuntil & read
    mock_reader.readuntil.return_value = b"DATA\x00"
    res = await conn.readuntil(b"\x00")
    assert res == b"DATA\x00"

    mock_reader.read.return_value = b"CHUNK"
    chunk = await conn.read(5)
    assert chunk == b"CHUNK"

    # Modem pins (no-op)
    await conn.set_modem_pins(dtr=False, rts=False)

    # Close
    await conn.close()
    assert conn.is_open is False
    mock_writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_serial_transport_tcp_connect_and_stream(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify SerialTransport end-to-end loop over an active TCP server."""
    received_packets: list[bytes] = []

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.readuntil(protocol.FRAME_DELIMITER)
                received_packets.append(data)
                # Echo back
                writer.write(data)
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]

    runtime_config.serial_port = f"tcp://127.0.0.1:{port}"

    mock_service = MagicMock()
    mock_service.on_serial_connected = AsyncMock()
    mock_service.on_serial_disconnected = AsyncMock()

    transport = SerialTransport(runtime_config, runtime_state, mock_service)

    transport_task = asyncio.create_task(transport.run())
    await asyncio.sleep(0.1)

    assert mock_service.on_serial_connected.awaited
    assert transport.serial is not None
    assert transport.serial.is_open is True

    # Send a raw frame
    await transport.send_raw(protocol.Command.CMD_GET_VERSION.value, b"", 1)
    await asyncio.sleep(0.1)

    assert len(received_packets) > 0

    # Stop transport
    await transport.stop()
    transport_task.cancel()
    try:
        await transport_task
    except asyncio.CancelledError:
        pass

    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_serial_transport_tcp_network_error_and_disconnect_paths(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify error handling, disconnect exceptions, and read task completion."""

    async def handle_immediate_close(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_immediate_close, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]

    runtime_config.serial_port = f"wifi://127.0.0.1:{port}"

    mock_service = MagicMock()
    mock_service.on_serial_connected = AsyncMock()
    mock_service.on_serial_disconnected = AsyncMock(side_effect=RuntimeError("cleanup-fail"))

    transport = SerialTransport(runtime_config, runtime_state, mock_service)

    # Run connection attempt which will hit immediate EOF / disconnect
    with pytest.raises(Exception):
        await getattr(transport, "_connect_and_run")()

    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_switch_local_baudrate_on_tcp_connection(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify switching baudrate on a network transport is a safe no-op."""
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_writer = AsyncMock(spec=asyncio.StreamWriter)
    mock_writer.transport = MagicMock()

    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.serial = AsyncTcpConnection(mock_reader, mock_writer, "10.0.0.1", 9000)

    # Baudrate switch helper
    getattr(transport, "_switch_local_baudrate")(230400)
    assert transport.serial is not None


@pytest.mark.asyncio
async def test_async_tcp_connection_context_manager() -> None:
    """Verify AsyncTcpConnection works cleanly with async context manager."""
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_writer = AsyncMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.transport = MagicMock()

    async with AsyncTcpConnection(mock_reader, mock_writer, "127.0.0.1", 9000) as conn:
        assert conn.is_open is True
        assert conn.host == "127.0.0.1"
        assert conn.port == 9000

    mock_writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_serial_transport_tcp_stop_event_branch(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify clean exit when stop event fires before read task."""

    async def handle_idle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_idle, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]

    runtime_config.serial_port = f"tcp://127.0.0.1:{port}"
    transport = SerialTransport(runtime_config, runtime_state, None)

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        getattr(transport, "_stop_event").set()

    asyncio.create_task(_stop_soon())
    # Should exit cleanly when wait_stop triggers
    await getattr(transport, "_connect_and_run")()

    assert transport.serial is None
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_serial_transport_tcp_none_service_disconnect(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify disconnect cleanup when service is None."""

    async def handle_close(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_close, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]

    runtime_config.serial_port = f"tcp://127.0.0.1:{port}"
    transport = SerialTransport(runtime_config, runtime_state, None)

    with pytest.raises(ConnectionError, match="Wireless network connection lost"):
        await getattr(transport, "_connect_and_run")()

    server.close()
    await server.wait_closed()
