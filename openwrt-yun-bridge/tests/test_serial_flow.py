"""Tests for SerialFlowController metrics integration."""
from __future__ import annotations

import asyncio
import logging
import struct

import pytest

from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.serial_flow import SerialFlowController
from yunbridge.state.context import RuntimeState


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
        struct.pack(">H", command_id),
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

    payload = runtime_state.consume_serial_flow_payload()
    assert payload is not None
    assert payload["commands_sent"] == 1
    assert payload["commands_acked"] == 1
    assert payload["retries"] == 0
    assert payload["failures"] == 0
    assert payload["last_event_unix"] > 0
    assert runtime_state.consume_serial_flow_payload() is None


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

    payload = runtime_state.consume_serial_flow_payload()
    assert payload is not None
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

    payload = runtime_state.consume_serial_flow_payload()
    assert payload is not None
    assert payload["commands_sent"] == 0
    assert payload["commands_acked"] == 0
    assert payload["retries"] == 0
    assert payload["failures"] == 1