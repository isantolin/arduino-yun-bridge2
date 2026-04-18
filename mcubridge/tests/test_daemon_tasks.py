"""Integration-style tests for daemon async tasks (SIL-2)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cobs import cobs
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.protocol import FRAME_DELIMITER, Command
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.transport import SerialTransport
from mcubridge.transport.mqtt import MqttTransport
from mcubridge.services.runtime import BridgeService


@pytest.mark.asyncio
async def test_serial_reader_task_processes_frame(monkeypatch: pytest.MonkeyPatch,
    runtime_config: RuntimeConfig,
    runtime_state: Any,
) -> None:
    state = runtime_state
    service = AsyncMock(spec=BridgeService)
    service.config = runtime_config
    service.state = state
    service.serial_connected = asyncio.Event()
    service.received_frames = []

    async def _on_connected() -> None:
        service.serial_connected.set()
    service.on_serial_connected.side_effect = _on_connected

    async def _handle_mcu(cmd: int, seq: int, pl: bytes):
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
            "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ),
        patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        patch.object(SerialTransport, "_toggle_dtr", AsyncMock()),
    ):
        transport: Any = SerialTransport(runtime_config, state, cast(Any, service))
        orig_run = SerialTransport._retryable_run.__wrapped__

        async def _limited_run(loop: Any) -> None:
            try:
                await orig_run(transport, loop) # type: ignore
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            raise RuntimeError("Break Loop")

        with patch.object(transport, "_retryable_run", _limited_run):
            task = asyncio.create_task(transport.run())
            await asyncio.wait_for(service.serial_connected.wait(), timeout=1)

            # Wait for frame
            for _ in range(50):
                if service.received_frames:
                    break
                await asyncio.sleep(0.01)

            assert service.received_frames
            cmd, _seq, pl = service.received_frames[0]
            assert cmd == Command.CMD_DIGITAL_READ_RESP.value
            assert pl == payload
            transport._stop_event # type: ignore # type: ignore.set()
            try:
                await asyncio.wait_for(task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError, RuntimeError):
                pass


@pytest.mark.asyncio
async def test_serial_reader_task_emits_crc_mismatch(monkeypatch: pytest.MonkeyPatch,
    runtime_config: RuntimeConfig,
    runtime_state: Any,
) -> None:
    state = runtime_state
    state.mark_transport_connected()
    state.mark_synchronized()
    service = AsyncMock(spec=BridgeService)
    service.config = runtime_config
    service.state = state
    service.serial_connected = asyncio.Event()

    async def _on_connected() -> None:
        service.serial_connected.set()
    service.on_serial_connected.side_effect = _on_connected

    frame = Frame(
        command_id=Command.CMD_DIGITAL_READ_RESP.value,
        sequence_id=0,
        payload=bytes([protocol.DIGITAL_HIGH]),
    ).build()
    corrupted = bytearray(cobs.encode(frame))
    corrupted[0] = protocol.UINT8_MASK
    encoded = bytes(corrupted) + FRAME_DELIMITER

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
            "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ),
        patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        patch.object(SerialTransport, "_toggle_dtr", AsyncMock()),
    ):
        transport: Any = SerialTransport(runtime_config, state, cast(Any, service))
        orig_run = SerialTransport._retryable_run.__wrapped__

        async def _limited_run(loop: Any) -> None:
            try:
                await orig_run(transport, loop) # type: ignore
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            raise RuntimeError("Break Loop")

        with patch.object(transport, "_retryable_run", _limited_run):
            task = asyncio.create_task(transport.run())
            await asyncio.wait_for(service.serial_connected.wait(), timeout=1)

            for _ in range(50):
                if state.serial_decode_errors > 0:
                    break
                await asyncio.sleep(0.01)

            assert state.serial_decode_errors > 0
            transport._stop_event # type: ignore # type: ignore.set()
            try:
                await asyncio.wait_for(task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError, RuntimeError):
                pass


@pytest.mark.asyncio
async def test_serial_reader_task_propagates_handshake_fatal(monkeypatch: pytest.MonkeyPatch,
    runtime_config: RuntimeConfig,
    runtime_state: Any,
) -> None:
    state = runtime_state
    service = AsyncMock(spec=BridgeService)
    service.on_serial_connected.side_effect = SerialHandshakeFatal("fatal-handshake")

    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader.readuntil.side_effect = asyncio.IncompleteReadError(b"", None)
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.wait_closed = AsyncMock()

    with (
        patch(
            "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ),
        patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        patch.object(SerialTransport, "_toggle_dtr", AsyncMock()),
    ):
        transport: Any = SerialTransport(runtime_config, state, cast(Any, service))
        task = asyncio.create_task(transport.run())

        try:
            await asyncio.wait_for(task, timeout=1)
        except SerialHandshakeFatal:
            pass
        except Exception as exc:
            pytest.fail(f"Expected SerialHandshakeFatal, got {type(exc)}")
        else:
            pytest.fail("Did not raise SerialHandshakeFatal")


@pytest.mark.asyncio
async def test_mqtt_task_handles_incoming_message(monkeypatch: pytest.MonkeyPatch,
    runtime_config: RuntimeConfig,
    runtime_state: Any,
) -> None:
    state = runtime_state
    state.mqtt_topic_prefix = runtime_config.mqtt_topic
    service = AsyncMock(spec=BridgeService)
    service.state = state
    service.handled = asyncio.Event()

    async def _handle_mqtt(inbound: Any) -> None:
        service.handled.set()
    service.handle_mqtt_message.side_effect = _handle_mqtt

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_msgs_ctx = AsyncMock()
    mock_client.messages = mock_msgs_ctx

    fake_msg = MagicMock()
    fake_msg.topic = f"{state.mqtt_topic_prefix}/console/in"
    fake_msg.payload = b"hi"
    fake_msg.qos = 0
    fake_msg.retain = False
    fake_msg.properties = None

    async def msg_gen():
        yield fake_msg
    mock_msgs_ctx.__aiter__.side_effect = msg_gen

    monkeypatch.setattr(
        "mcubridge.transport.mqtt.aiomqtt.Client",
        lambda **_kw: mock_client # type: ignore,
    )

    runtime_config.mqtt_tls = False
    task = asyncio.create_task(
        MqttTransport(runtime_config, state, cast(Any, service)).run()
    )

    try:
        await asyncio.wait_for(service.handled.wait(), timeout=1)
    finally:
        task.cancel()
        try:
            await task
        except* asyncio.CancelledError:
            pass
