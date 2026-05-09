"""Serial flow control tests for McuBridge."""

from __future__ import annotations

import asyncio
import logging

import msgspec

from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, Status
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState


async def _send_ack(controller: SerialFlowController, command_id: int) -> None:
    """Helper to simulate an ACK from the MCU."""
    await asyncio.sleep(0.01)
    controller.on_frame_received(
        Status.ACK.value, 0, msgspec.msgpack.encode(structures.AckPacket(command_id))
    )


def test_serial_flow_success_path(
    runtime_state: RuntimeState,
) -> None:
    serial_flow_logger = logging.getLogger("mcubridge.test.flow")

    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )
        controller.set_pipeline_observer(runtime_state.record_serial_pipeline_event)

        async def fake_sender(
            command_id: int, payload: bytes, seq_id: int | None = None
        ) -> bool:
            asyncio.create_task(_send_ack(controller, command_id))
            return True

        controller.set_sender(fake_sender)

        result = await controller.send(Command.CMD_DIGITAL_WRITE.value, b"")
        assert result is True

    asyncio.run(_run())

    snapshot = runtime_state.build_bridge_snapshot().serial_flow
    assert snapshot.commands_sent == 1
    assert snapshot.commands_acked == 1
    assert snapshot.retries == 0
    assert snapshot.failures == 0


def test_serial_flow_records_retry_metrics(
    runtime_state: RuntimeState,
) -> None:
    serial_flow_logger = logging.getLogger("mcubridge.test.flow")

    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=3,
            logger=serial_flow_logger,
        )
        controller.set_pipeline_observer(runtime_state.record_serial_pipeline_event)

        attempts = 0

        async def fake_sender(
            command_id: int, payload: bytes, seq_id: int | None = None
        ) -> bool:
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

    snapshot = runtime_state.build_bridge_snapshot().serial_flow
    # [SIL-2] Both attempts are counted
    assert snapshot.commands_sent == 2
    assert snapshot.commands_acked == 1
    assert snapshot.retries == 1
    assert snapshot.failures == 0


def test_serial_flow_records_failure_metrics(
    runtime_state: RuntimeState,
) -> None:
    serial_flow_logger = logging.getLogger("mcubridge.test.flow")

    async def _run() -> None:
        controller = SerialFlowController(
            ack_timeout=0.05,
            response_timeout=0.1,
            max_attempts=1,
            logger=serial_flow_logger,
        )
        controller.set_pipeline_observer(runtime_state.record_serial_pipeline_event)

        async def fake_sender(
            command_id: int, payload: bytes, seq_id: int | None = None
        ) -> bool:
            return False

        controller.set_sender(fake_sender)

        result = await controller.send(Command.CMD_DIGITAL_WRITE.value, b"")
        assert result is False

    asyncio.run(_run())

    snapshot = runtime_state.build_bridge_snapshot().serial_flow
    assert snapshot.commands_sent == 1
    assert snapshot.commands_acked == 0
    assert snapshot.retries == 0
    assert snapshot.failures == 1
