"""Tests for SerialFlowController metrics integration."""

from __future__ import annotations

import asyncio
import logging
import struct

import pytest

from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import Command, Status, UINT16_FORMAT
from mcubridge.services.serial_flow import SerialFlowController, _status_name
from mcubridge.state.context import RuntimeState


@pytest.fixture()
def serial_flow_logger() -> logging.Logger:
    return logging.getLogger("test.serial_flow")


async def _send_ack(
    controller: SerialFlowController,
    command_id: int,
    delay: float = 0.0,
) -> None:
    if delay:
        await asyncio.sleep(delay)
    controller.on_frame_received(
        Status.ACK.value,
        struct.pack(UINT16_FORMAT, command_id),
    )


def test_serial_flow_records_success_metrics(
    runtime_state: RuntimeState,
    serial_flow_logger: logging.Logger,
) -> None:
    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
            metrics_callback=runtime_state.record_serial_flow_event,
        )

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            asyncio.create_task(_send_ack(controller, command_id))
            return True

        controller.set_sender(fake_sender)

        result = await controller.send(Command.CMD_DIGITAL_WRITE.value, b"")
        assert result is True

    asyncio.run(_run())

    payload = runtime_state.build_bridge_snapshot()["serial_flow"]
    assert payload["commands_sent"] == 1
    assert payload["commands_acked"] == 1
    assert payload["retries"] == 0
    assert payload["failures"] == 0
    assert payload["last_event_unix"] > 0


def test_serial_flow_records_retry_metrics(
    runtime_state: RuntimeState,
    serial_flow_logger: logging.Logger,
) -> None:
    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=3,
            logger=serial_flow_logger,
            metrics_callback=runtime_state.record_serial_flow_event,
        )

        attempts = 0

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                asyncio.create_task(_send_ack(controller, command_id))
            return True

        controller.set_sender(fake_sender)

        result = await controller.send(Command.CMD_DIGITAL_WRITE.value, b"")
        assert result is True
        assert attempts == 2

    asyncio.run(_run())

    payload = runtime_state.build_bridge_snapshot()["serial_flow"]
    assert payload["commands_sent"] == 2
    assert payload["commands_acked"] == 1
    assert payload["retries"] == 1
    assert payload["failures"] == 0


def test_serial_flow_records_failure_metrics(
    runtime_state: RuntimeState,
    serial_flow_logger: logging.Logger,
) -> None:
    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
            metrics_callback=runtime_state.record_serial_flow_event,
        )

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            return False

        controller.set_sender(fake_sender)

        result = await controller.send(Command.CMD_DIGITAL_WRITE.value, b"")
        assert result is False

    asyncio.run(_run())

    payload = runtime_state.build_bridge_snapshot()["serial_flow"]
    assert payload["commands_sent"] == 0
    assert payload["commands_acked"] == 0
    assert payload["retries"] == 0
    assert payload["failures"] == 1


def test_serial_flow_rejects_without_sender(
    serial_flow_logger: logging.Logger,
) -> None:
    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )
        result = await controller.send(Command.CMD_DIGITAL_WRITE.value, b"")
        assert result is False

    asyncio.run(_run())


def test_serial_flow_reset_abandons_pending(
    serial_flow_logger: logging.Logger,
) -> None:
    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )

        sender_called = asyncio.Event()

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sender_called.set()
            return True

        controller.set_sender(fake_sender)

        send_task = asyncio.create_task(controller.send(Command.CMD_DIGITAL_READ.value, b""))
        await sender_called.wait()
        await controller.reset()
        assert await send_task is False

    asyncio.run(_run())


def test_serial_flow_handles_failure_status(
    serial_flow_logger: logging.Logger,
) -> None:
    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )

        loop = asyncio.get_running_loop()

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            loop.call_soon(
                controller.on_frame_received,
                Status.ERROR.value,
                b"",
            )
            return True

        controller.set_sender(fake_sender)

        result = await controller.send(Command.CMD_DIGITAL_WRITE.value, b"")
        assert result is False

    asyncio.run(_run())


def test_serial_flow_acknowledges_ack_only_command(
    serial_flow_logger: logging.Logger,
) -> None:
    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )

        loop = asyncio.get_running_loop()

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            loop.call_soon(
                controller.on_frame_received,
                Status.ACK.value,
                command_id.to_bytes(2, "big"),
            )
            return True

        controller.set_sender(fake_sender)

        result = await controller.send(Command.CMD_CONSOLE_WRITE.value, b"")
        assert result is True

    asyncio.run(_run())


def test_serial_flow_handles_response_after_ack(
    serial_flow_logger: logging.Logger,
) -> None:
    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )

        loop = asyncio.get_running_loop()
        command_id = Command.CMD_DIGITAL_READ.value

        async def fake_sender(_cid: int, payload: bytes) -> bool:
            def emit_frames() -> None:
                controller.on_frame_received(
                    Status.ACK.value,
                    command_id.to_bytes(2, "big"),
                )
                controller.on_frame_received(
                    Command.CMD_DIGITAL_READ_RESP.value,
                    bytes([protocol.DIGITAL_HIGH]),
                )

            loop.call_soon(emit_frames)
            return True

        controller.set_sender(fake_sender)

        result = await controller.send(command_id, b"")
        assert result is True

    asyncio.run(_run())


def test_serial_flow_retries_on_mismatched_ack(
    serial_flow_logger: logging.Logger,
) -> None:
    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.01,
            response_timeout=0.05,
            max_attempts=1,
            logger=serial_flow_logger,
        )

        loop = asyncio.get_running_loop()

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            def emit_wrong_ack() -> None:
                other_cmd = Command.CMD_DIGITAL_WRITE.value
                controller.on_frame_received(
                    Status.ACK.value,
                    other_cmd.to_bytes(2, "big"),
                )

            loop.call_soon(emit_wrong_ack)
            return True

        controller.set_sender(fake_sender)

        result = await controller.send(Command.CMD_CONSOLE_WRITE.value, b"")
        assert result is False

    asyncio.run(_run())


def test_serial_flow_emits_pipeline_events(
    serial_flow_logger: logging.Logger,
) -> None:
    events: list[dict[str, object]] = []

    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )
        controller.set_pipeline_observer(events.append)

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            asyncio.create_task(_send_ack(controller, command_id))
            return True

        controller.set_sender(fake_sender)

        result = await controller.send(Command.CMD_DIGITAL_WRITE.value, b"")
        assert result is True

    asyncio.run(_run())

    names = [event["event"] for event in events]
    assert names == ["start", "ack", "success"]


def test_serial_flow_pipeline_failure_event(
    serial_flow_logger: logging.Logger,
) -> None:
    events: list[dict[str, object]] = []

    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )
        controller.set_pipeline_observer(events.append)

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            return False

        controller.set_sender(fake_sender)
        result = await controller.send(Command.CMD_DIGITAL_WRITE.value, b"")
        assert result is False

    asyncio.run(_run())

    assert events[-1]["event"] == "failure"


def test_serial_flow_pipeline_abandoned_on_reset(
    serial_flow_logger: logging.Logger,
) -> None:
    events: list[dict[str, object]] = []

    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )
        controller.set_pipeline_observer(events.append)

        sender_called = asyncio.Event()

        async def fake_sender(command_id: int, payload: bytes) -> bool:
            sender_called.set()
            return True

        controller.set_sender(fake_sender)

        send_task = asyncio.create_task(controller.send(Command.CMD_DIGITAL_READ.value, b""))
        await sender_called.wait()
        await controller.reset()
        assert await send_task is False

    asyncio.run(_run())

    event_names = [event["event"] for event in events]
    assert "abandoned" in event_names


def test_status_name_handles_unknown() -> None:
    assert _status_name(None) == "unknown"
    assert _status_name(Status.OK.value) == "OK"
    assert _status_name(153) == "0x99"
