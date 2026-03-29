"""Integration-style tests for daemon async tasks."""

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
from mcubridge.state.context import create_runtime_state
from mcubridge.transport import (
    SerialTransport,
)
from mcubridge.transport.mqtt import MqttTransport

# [REDUCTION] Use shared mocks to avoid duplication
from tests.mocks import MockFatalSerialService, MockMQTTService, MockSerialService


@pytest.mark.asyncio
async def test_serial_reader_task_processes_frame(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    service = MockSerialService(runtime_config, state)

    payload = bytes([protocol.DIGITAL_HIGH])
    frame = Frame(command_id=Command.CMD_DIGITAL_READ_RESP.value, sequence_id=0, payload=payload).build()
    encoded = cobs.encode(frame) + FRAME_DELIMITER

    # Mock Streams API
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader.readuntil.side_effect = [encoded, asyncio.IncompleteReadError(b"", None)]

    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.wait_closed = AsyncMock()

    async def _mock_toggle_dtr(_self: Any, _loop: Any) -> None:
        pass

    with (
        patch(
            "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ),
        patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        patch.object(SerialTransport, "_toggle_dtr", _mock_toggle_dtr),
    ):
        # Patch tenacity retry to fail after first attempt to avoid infinite loops
        orig_run = SerialTransport._retryable_run.__wrapped__
        with patch.object(SerialTransport, "_retryable_run"):
            transport = SerialTransport(runtime_config, state, cast(Any, service))

            # We want to break the loop after one failure
            async def _limited_run(loop):
                try:
                    # Use the captured original function
                    await orig_run(transport, loop)
                except (ConnectionError, asyncio.IncompleteReadError):
                    # expected first failure
                    pass
                raise RuntimeError("Break Loop")

            with patch.object(transport, "_retryable_run", _limited_run):
                task = asyncio.create_task(transport.run())

                await asyncio.wait_for(service.serial_connected.wait(), timeout=1)


                # Wait for frames to be processed (actual event loop yielding)
                for _ in range(50):
                    if service.received_frames:
                        break
                    await asyncio.sleep(0.01)

                assert service.received_frames
                command_id, seq_id, received_payload = service.received_frames[0]
                assert command_id == Command.CMD_DIGITAL_READ_RESP.value
                assert seq_id == 0
                assert received_payload == payload
                transport._stop_event.set()
                try:
                    await asyncio.wait_for(task, timeout=0.5)
                except (asyncio.TimeoutError, asyncio.CancelledError, RuntimeError):
                    pass


@pytest.mark.asyncio
async def test_serial_reader_task_emits_crc_mismatch(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    state.mark_transport_connected()
    state.mark_synchronized()
    service = MockSerialService(runtime_config, state)

    frame = Frame(
        command_id=Command.CMD_DIGITAL_READ_RESP.value,
        sequence_id=0,
        payload=bytes([protocol.DIGITAL_HIGH]),
    ).build()
    corrupted = bytearray(cobs.encode(frame))
    corrupted[0] = protocol.UINT8_MASK  # Invalid COBS code
    encoded = bytes(corrupted) + FRAME_DELIMITER

    # Mock Streams API
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader.readuntil.side_effect = [encoded, asyncio.IncompleteReadError(b"", None)]

    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.wait_closed = AsyncMock()

    async def _mock_toggle_dtr(_self: Any, _loop: Any) -> None:
        pass

    with (
        patch(
            "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ),
        patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        patch.object(SerialTransport, "_toggle_dtr", _mock_toggle_dtr),
    ):
        transport = SerialTransport(runtime_config, state, cast(Any, service))

        orig_run = SerialTransport._retryable_run.__wrapped__
        async def _limited_run(loop):
            try:
                await orig_run(transport, loop)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            raise RuntimeError("Break Loop")


        with patch.object(transport, "_retryable_run", _limited_run):
            task = asyncio.create_task(transport.run())

            await asyncio.wait_for(service.serial_connected.wait(), timeout=1)


            # Wait for state to update
            for _ in range(50):
                if state.serial_decode_errors > 0:
                    break
                await asyncio.sleep(0.01)

            assert not service.received_frames
            assert state.serial_decode_errors > 0

            transport._stop_event.set()
            try:
                await asyncio.wait_for(task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError, RuntimeError):
                pass


@pytest.mark.asyncio
async def test_serial_reader_task_limits_packet_size(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    state.mark_transport_connected()
    state.mark_synchronized()
    service = MockSerialService(runtime_config, state)

    # Mock Streams API
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader.readuntil.side_effect = [
        asyncio.LimitOverrunError("Too long", 0),
        asyncio.IncompleteReadError(b"", None)
    ]

    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.wait_closed = AsyncMock()

    async def _mock_toggle_dtr(_self: Any, _loop: Any) -> None:
        pass

    with (
        patch(
            "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ),
        patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        patch.object(SerialTransport, "_toggle_dtr", _mock_toggle_dtr),
    ):
        transport = SerialTransport(runtime_config, state, cast(Any, service))

        orig_run = SerialTransport._retryable_run.__wrapped__
        async def _limited_run(loop):
            try:
                await orig_run(transport, loop)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            raise RuntimeError("Break Loop")


        with patch.object(transport, "_retryable_run", _limited_run):
            task = asyncio.create_task(transport.run())

            await asyncio.wait_for(service.serial_connected.wait(), timeout=1)


            for _ in range(50):
                if state.serial_decode_errors >= 1:
                    break
                await asyncio.sleep(0.01)

            assert not service.received_frames
            assert state.serial_decode_errors >= 1

            transport._stop_event.set()
            try:
                await asyncio.wait_for(task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError, RuntimeError):
                pass


@pytest.mark.asyncio
async def test_serial_reader_task_propagates_handshake_fatal(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    service = MockFatalSerialService(runtime_config, state)

    # Mock Streams API
    mock_reader = AsyncMock(spec=asyncio.StreamReader)
    mock_reader.readuntil.side_effect = asyncio.IncompleteReadError(b"", None)

    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.is_closing.return_value = False
    mock_writer.wait_closed = AsyncMock()

    async def _mock_toggle_dtr(_self: Any, _loop: Any) -> None:
        pass

    with (
        patch(
            "mcubridge.transport.serial.serial_asyncio_fast.open_serial_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ),
        patch("mcubridge.transport.serial.serial.Serial", MagicMock()),
        patch.object(SerialTransport, "_toggle_dtr", _mock_toggle_dtr),
    ):
        transport = SerialTransport(runtime_config, state, cast(Any, service))
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
async def test_mqtt_task_handles_incoming_message(
    monkeypatch: pytest.MonkeyPatch, runtime_config: RuntimeConfig
) -> None:
    state = create_runtime_state(runtime_config)
    state.mqtt_topic_prefix = runtime_config.mqtt_topic
    service = MockMQTTService(state)

    # Mock aiomqtt Client
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    # Mock messages iterator
    mock_msgs_ctx = AsyncMock()
    mock_client.messages = mock_msgs_ctx

    # Mock iterator
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
        lambda **_kw: mock_client,
    )

    runtime_config.mqtt_tls = False

    task = asyncio.create_task(MqttTransport(runtime_config, state, cast(Any, service)).run())

    try:
        await asyncio.wait_for(service.handled.wait(), timeout=1)
    finally:
        task.cancel()
        try:
            await task
        except* asyncio.CancelledError:
            pass
