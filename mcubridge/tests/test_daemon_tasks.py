"""Unit tests for daemon serial tasks and lifecycle (SIL-2)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cobs import cobs

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, FRAME_DELIMITER
from mcubridge.protocol.frame import Frame
from mcubridge.transport.serial import SerialTransport
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.services.runtime import BridgeService
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_serial_reader_task_processes_frame(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        service = AsyncMock(spec=BridgeService)
        service.config = runtime_config
        service.state = state
        service.serial_connected = asyncio.Event()
        service.received_frames = []

        async def _on_connected() -> None:
            service.serial_connected.set()
        service.on_serial_connected.side_effect = _on_connected

        async def _handle_mcu(cmd: int, seq: int, pl: bytes) -> None:
            service.received_frames.append((cmd, seq, pl))
        service.handle_mcu_frame.side_effect = _handle_mcu

        payload = bytes([protocol.DIGITAL_HIGH])
        frame = Frame(
            command_id=Command.CMD_DIGITAL_READ_RESP.value,
            sequence_id=0,
            payload=payload,
        ).build()
        encoded = cobs.encode(frame) + FRAME_DELIMITER

        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_reader.readuntil.side_effect = [
            encoded,
            asyncio.IncompleteReadError(b"", None),
        ]
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        mock_writer.wait_closed = AsyncMock()

        with (
            patch(
                "serial_asyncio_fast.open_serial_connection",
                AsyncMock(return_value=(mock_reader, mock_writer)),
            ),
            patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        ):
            transport = SerialTransport(runtime_config, state, cast(Any, service))
            await transport._retryable_run(asyncio.get_running_loop()) # type: ignore[reportPrivateUsage]

        assert len(service.received_frames) == 1
        assert service.received_frames[0][0] == Command.CMD_DIGITAL_READ_RESP.value
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_serial_reader_task_emits_crc_mismatch(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        state.mark_transport_connected()
        state.mark_synchronized()
        service = AsyncMock(spec=BridgeService)
        service.config = runtime_config
        service.state = state
        service.serial_connected = asyncio.Event()

        async def _on_connected() -> None:
            service.serial_connected.set()
        service.on_serial_connected.side_effect = _on_connected

        # Create a frame with wrong checksum
        raw = bytearray(
            Frame(
                command_id=Command.CMD_DIGITAL_READ_RESP.value,
                sequence_id=0,
                payload=bytes([protocol.DIGITAL_HIGH]),
            ).build()
        )
        raw[-1] ^= 0xFF
        encoded = cobs.encode(bytes(raw)) + FRAME_DELIMITER

        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_reader.readuntil.side_effect = [
            encoded,
            asyncio.IncompleteReadError(b"", None),
        ]
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        mock_writer.wait_closed = AsyncMock()

        with (
            patch(
                "serial_asyncio_fast.open_serial_connection",
                AsyncMock(return_value=(mock_reader, mock_writer)),
            ),
            patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        ):
            transport = SerialTransport(runtime_config, state, cast(Any, service))
            await transport._retryable_run(asyncio.get_running_loop()) # type: ignore[reportPrivateUsage]

        assert state.serial_crc_errors >= 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_serial_reader_task_propagates_handshake_fatal(
    runtime_config: RuntimeConfig,
) -> None:
    state = create_runtime_state(runtime_config)
    try:
        service = AsyncMock(spec=BridgeService)
        service.on_serial_connected.side_effect = SerialHandshakeFatal("fatal-handshake")

        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_reader.readuntil.side_effect = asyncio.IncompleteReadError(b"", None)
        mock_writer = MagicMock(spec=asyncio.StreamWriter)
        mock_writer.is_closing.return_value = False
        mock_writer.wait_closed = AsyncMock()

        with (
            patch(
                "serial_asyncio_fast.open_serial_connection",
                AsyncMock(return_value=(mock_reader, mock_writer)),
            ),
            patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        ):
            transport = SerialTransport(runtime_config, state, cast(Any, service))
            # Should propagate SerialHandshakeFatal
            with pytest.raises(SerialHandshakeFatal):
                await transport._retryable_run(asyncio.get_running_loop()) # type: ignore[reportPrivateUsage]
    finally:
        state.cleanup()
