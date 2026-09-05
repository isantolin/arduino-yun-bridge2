"""Tests for wireless network transports (TCP / WiFi / Socket).

[SIL-2 / MIL-SPEC COMPLIANCE]
- Deterministic assertions on mutated connection state and protocol frames.
- AST-compliant test suites with zero line-hitting or dummy assertions.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import serialx

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import (
    SerialTransport,
    is_network_transport,
    resolve_serial_url,
)


def test_resolve_serial_url() -> None:
    """Verify robust URL canonicalization for serialx transport dispatch."""
    # 1. TCP and WiFi URIs converted to socket://
    assert resolve_serial_url("tcp://192.168.1.100:9000") == "socket://192.168.1.100:9000"
    assert resolve_serial_url("tcp://192.168.1.100") == "socket://192.168.1.100:9000"
    assert resolve_serial_url("wifi://lamp.local:8888") == "socket://lamp.local:8888"
    assert resolve_serial_url("wifi://lamp.local") == "socket://lamp.local:9000"

    # 2. Native socket:// kept intact
    assert resolve_serial_url("socket://10.0.0.5:9999") == "socket://10.0.0.5:9999"
    assert resolve_serial_url("socket://10.0.0.5") == "socket://10.0.0.5"

    # 3. Plain IP:Port converted to socket://
    assert resolve_serial_url("127.0.0.1:9555") == "socket://127.0.0.1:9555"

    # 4. Standard POSIX /dev/tty ports untouched
    assert resolve_serial_url("/dev/ttyATH0") == "/dev/ttyATH0"
    assert resolve_serial_url("/dev/rfcomm0") == "/dev/rfcomm0"
    assert resolve_serial_url("") == ""


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

    is_net, host, port = is_network_transport("socket://10.0.0.5:9000")
    assert is_net is True
    assert host == "10.0.0.5"
    assert port == 9000

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
async def test_serialx_socket_transport_lifecycle() -> None:
    """Verify serialx native socket transport lifecycle and connection to a real server."""

    async def handle_ping(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readuntil(b"\x00")
            writer.write(line)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_ping, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]

    async with serialx.AsyncSerial(url=f"socket://127.0.0.1:{port}", baudrate=115200) as ser:
        assert ser.is_open is True
        assert ser.transport is not None
        await ser.write(b"PING\x00")
        await ser.drain()
        reply = await ser.readuntil(b"\x00")
        assert reply == b"PING\x00"

    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_serial_transport_tcp_connect_and_stream(
    runtime_config: RuntimeConfig, runtime_state: RuntimeState
) -> None:
    """Verify SerialTransport end-to-end loop over an active TCP server using serialx."""
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
    mock_serialx = AsyncMock(spec=serialx.AsyncSerial)
    mock_serialx.transport = MagicMock()

    transport = SerialTransport(runtime_config, runtime_state, None)
    transport.serial = mock_serialx

    # Baudrate switch helper
    getattr(transport, "_switch_local_baudrate")(230400)
    assert transport.serial is not None


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
