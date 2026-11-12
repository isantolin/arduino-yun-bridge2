"""Exhaustive tests for mcubridge.transport.serial module. [SIL-2]"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import protocol
from mcubridge.security.security import LinkCipher
from mcubridge.state.context import RuntimeState
from mcubridge.transport.serial import SerialTransport


@pytest.fixture
def mock_serial_setup() -> tuple[SerialTransport, RuntimeState, MagicMock]:
    config = load_runtime_config({"serial_port": "/dev/ttyMOCK", "serial_baud": 115200})
    state = RuntimeState()
    service = AsyncMock()
    transport = SerialTransport(config, state, service)

    mock_serial = MagicMock()
    mock_serial.baudrate = 115200
    mock_serial.read_until = MagicMock(return_value=b"")
    mock_serial.write = MagicMock(return_value=None)
    transport.serial = mock_serial
    state.serial_writer = mock_serial
    state.serial_tx_allowed.set()

    return transport, state, mock_serial


def _install_test_cipher(transport: SerialTransport) -> None:
    if transport.config.serial_shared_secret:
        nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
        transport.state.link_cipher = LinkCipher.for_host(transport.config.serial_shared_secret, nonce)


async def test_pw_transfer_client_is_shared_and_nonblocking(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    mock_serial.is_open = True
    instances: list[object] = []

    class FakeTransferManager:
        last_write: tuple[int, bytes, int] | None = None

        def __init__(self, service: object, *, max_chunk_size_bytes: int) -> None:
            self.service = service
            self.max_chunk_size_bytes = max_chunk_size_bytes
            instances.append(self)

        def read(self, resource_id: int, *, initial_offset: int = 0) -> bytes:
            return f"{resource_id}:{initial_offset}".encode()

        def write(self, resource_id: int, data: bytes, *, initial_offset: int = 0) -> None:
            self.last_write = (resource_id, data, initial_offset)

    monkeypatch.setattr("mcubridge.transport.serial.pw_transfer.Manager", FakeTransferManager)

    data = await transport.transfer_read(0x4D454741, offset=7)
    await transport.transfer_write(0x4D454741, b"data", offset=9)

    assert data == b"1296385857:7"
    assert len(instances) == 1
    manager = instances[0]
    assert isinstance(manager, FakeTransferManager)
    assert manager.max_chunk_size_bytes == 128
    assert manager.last_write == (0x4D454741, b"data", 9)


async def test_pw_transfer_cancellation_keeps_role_lock_until_worker_finishes(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    mock_serial.is_open = True
    worker_started = threading.Event()
    release_worker = threading.Event()
    writes: list[tuple[int, bytes]] = []

    class BlockingTransferManager:
        def __init__(self, _service: object, *, max_chunk_size_bytes: int) -> None:
            assert max_chunk_size_bytes == 128

        def read(self, _resource_id: int) -> bytes:
            worker_started.set()
            release_worker.wait(timeout=2.0)
            return b""

        def write(self, resource_id: int, data: bytes) -> None:
            writes.append((resource_id, data))

    monkeypatch.setattr("mcubridge.transport.serial.pw_transfer.Manager", BlockingTransferManager)

    first = asyncio.create_task(transport.transfer_read(0x4D454741))
    assert await asyncio.to_thread(worker_started.wait, 1.0)
    first.cancel()
    second = asyncio.create_task(transport.transfer_write(0x4D454741, b"next"))
    await asyncio.sleep(0.05)
    assert writes == []

    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert writes == [(0x4D454741, b"next")]


async def test_pw_transfer_rejects_disconnected_link(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    mock_serial.is_open = False

    with pytest.raises(ConnectionError, match="disconnected"):
        await transport.transfer_read(0x4D454741)


def test_switch_local_baudrate_failure(mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock]) -> None:
    transport, _state, mock_serial = mock_serial_setup
    type(mock_serial).baudrate = PropertyMock(side_effect=OSError("Baudrate error"))

    switch_fn = transport.switch_local_baudrate
    with pytest.raises(RuntimeError, match="UART access failed"):
        switch_fn(9600)


async def test_read_loop_decode_error(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, state, mock_serial = mock_serial_setup

    # First call returns invalid HDLC packet + delimiter, second raises CancelledError to end loop
    mock_serial.read_until.side_effect = [
        b"\x7e\xff\xff\x7e",
        asyncio.CancelledError(),
    ]

    read_loop_fn = transport.read_loop
    with pytest.raises(asyncio.CancelledError):
        await read_loop_fn(mock_serial)
    assert state.serial_decode_errors >= 1


async def test_read_loop_crc_error_recovery(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup

    # Invalid CRC on HDLC frame
    bad_frame = b"\x7e\x01\x03" + b"x" * 20 + b"\x12\x34\x56\x78\x7e"

    mock_serial.read_until.side_effect = [
        bad_frame,
        asyncio.CancelledError(),
    ]

    read_loop_fn = transport.read_loop
    with pytest.raises(asyncio.CancelledError):
        await read_loop_fn(mock_serial)
    assert transport.consecutive_crc_errors == 1


async def test_read_loop_valid_frame_dispatch(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup

    hdlc_frame = protocol.encode_hdlc_console(b"test_payload")

    mock_serial.read_until.side_effect = [
        hdlc_frame,
        asyncio.CancelledError(),
    ]

    read_loop_fn = transport.read_loop
    with pytest.raises(asyncio.CancelledError):
        await read_loop_fn(mock_serial)
    assert transport.consecutive_crc_errors == 0


async def test_send_raw_success(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    _install_test_cipher(transport)
    mock_serial.is_open = True

    res = await transport.send_raw(command_id=0x01, payload=b"test")
    assert res is True
    assert mock_serial.write.call_count == 1
    written_data = mock_serial.write.call_args[0][0]
    assert written_data.endswith(protocol.FRAME_DELIMITER)


async def test_send_raw_write_error(
    mock_serial_setup: tuple[SerialTransport, RuntimeState, MagicMock],
) -> None:
    transport, _state, mock_serial = mock_serial_setup
    _install_test_cipher(transport)
    mock_serial.is_open = True
    mock_serial.write.side_effect = OSError("Write failed")

    res = await transport.send_raw(command_id=0x01, payload=b"test")
    assert res is False
