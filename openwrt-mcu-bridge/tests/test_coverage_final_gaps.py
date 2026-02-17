"""Tests targeting coverage gaps in task_supervisor, serial_flow, and payloads."""

from __future__ import annotations

import logging

import pytest
from mcubridge.protocol.protocol import Status
from mcubridge.services.serial_flow import PendingCommand, SerialFlowController
from mcubridge.services.shell import (
    PayloadValidationError,
    ShellCommandPayload,
    ShellPidPayload,
)

# ============================================================================
# Payload Tests
# ============================================================================


def test_shell_command_empty_json_command() -> None:
    """Cover ShellCommandPayload with empty command in JSON."""
    with pytest.raises(PayloadValidationError, match="empty"):
        ShellCommandPayload.from_mqtt(b'{"command": "  "}')


def test_shell_command_malformed_json() -> None:
    """Cover malformed JSON fallback to plain text."""
    result = ShellCommandPayload.from_mqtt(b'{malformed json here')
    assert result.command == "{malformed json here"


def test_shell_command_too_long() -> None:
    """Cover command length validation."""
    long_cmd = b"x" * 600
    with pytest.raises(PayloadValidationError, match="512"):
        ShellCommandPayload.from_mqtt(long_cmd)


def test_shell_pid_not_integer() -> None:
    """Cover non-integer PID parsing."""
    with pytest.raises(PayloadValidationError, match="integer"):
        ShellPidPayload.from_topic_segment("abc")


def test_shell_pid_zero() -> None:
    """Cover PID zero validation."""
    with pytest.raises(PayloadValidationError, match="positive"):
        ShellPidPayload.from_topic_segment("0")


def test_shell_pid_too_large() -> None:
    """Cover PID > 65535."""
    with pytest.raises(PayloadValidationError, match="65535"):
        ShellPidPayload.from_topic_segment("99999")


def test_shell_pid_valid() -> None:
    """Cover valid PID parsing."""
    result = ShellPidPayload.from_topic_segment("1234")
    assert result.pid == 1234


# ============================================================================
# Serial Flow Controller Tests
# ============================================================================


@pytest.mark.asyncio
async def test_serial_flow_no_sender() -> None:
    """Cover send when sender is not set."""
    controller = SerialFlowController(
        ack_timeout=1.0,
        response_timeout=2.0,
        max_attempts=3,
        logger=logging.getLogger("test"),
    )
    # No sender set
    result = await controller.send(0x01, b"test")
    assert result is False


@pytest.mark.asyncio
async def test_serial_flow_reset_with_pending() -> None:
    """Cover reset when a command is pending."""
    controller = SerialFlowController(
        ack_timeout=1.0,
        response_timeout=2.0,
        max_attempts=3,
        logger=logging.getLogger("test"),
    )

    # Manually set up a pending command
    pending = PendingCommand(command_id=0x01, expected_resp_ids={0x81})
    async with controller._condition:
        controller._current = pending

    await controller.reset()

    assert pending.success is False
    assert pending.completion.is_set()


@pytest.mark.asyncio
async def test_serial_flow_on_frame_ack_wrong_target() -> None:
    """Cover ACK frame with wrong target command."""
    controller = SerialFlowController(
        ack_timeout=1.0,
        response_timeout=2.0,
        max_attempts=3,
        logger=logging.getLogger("test"),
    )

    pending = PendingCommand(command_id=0x01, expected_resp_ids={0x81})
    controller._current = pending

    # ACK for different command
    controller.on_frame_received(Status.ACK.value, b"\x00\x02")  # ACK for cmd 0x02

    assert pending.ack_received is False


@pytest.mark.asyncio
async def test_serial_flow_failure_status_printable_ignored() -> None:
    """Cover failure status with printable payload (ignored)."""
    controller = SerialFlowController(
        ack_timeout=1.0,
        response_timeout=2.0,
        max_attempts=3,
        logger=logging.getLogger("test"),
    )

    pending = PendingCommand(command_id=0x01, expected_resp_ids={0x81})
    controller._current = pending

    # Failure status with printable text - should be ignored
    controller.on_frame_received(
        Status.ERROR.value,  # Generic error
        b"serial_rx_overflow"
    )

    # Not marked as failure because payload is printable
    assert pending.success is None


@pytest.mark.asyncio
async def test_serial_flow_success_status() -> None:
    """Cover success status code handling."""
    controller = SerialFlowController(
        ack_timeout=1.0,
        response_timeout=2.0,
        max_attempts=3,
        logger=logging.getLogger("test"),
    )

    pending = PendingCommand(command_id=0x01, expected_resp_ids=set())
    controller._current = pending

    controller.on_frame_received(Status.OK.value, b"")

    assert pending.success is True


def test_serial_flow_metrics_callback() -> None:
    """Cover metrics callback."""
    events = []
    controller = SerialFlowController(
        ack_timeout=1.0,
        response_timeout=2.0,
        max_attempts=3,
        logger=logging.getLogger("test"),
        metrics_callback=events.append,
    )

    controller._emit_metric("test_event")
    assert "test_event" in events


def test_serial_flow_pipeline_observer() -> None:
    """Cover pipeline observer."""
    observations = []
    controller = SerialFlowController(
        ack_timeout=1.0,
        response_timeout=2.0,
        max_attempts=3,
        logger=logging.getLogger("test"),
    )
    controller.set_pipeline_observer(observations.append)

    pending = PendingCommand(command_id=0x01)
    controller._notify_pipeline("test", pending, status=None)

    assert len(observations) == 1
    assert observations[0]["event"] == "test"


# ============================================================================
# Pending Command Tests
# ============================================================================


def test_pending_command_mark_success() -> None:
    """Cover PendingCommand.mark_success()."""
    pending = PendingCommand(command_id=0x01)
    pending.mark_success()

    assert pending.success is True
    assert pending.completion.is_set()


def test_pending_command_mark_failure() -> None:
    """Cover PendingCommand.mark_failure()."""
    pending = PendingCommand(command_id=0x01)
    pending.mark_failure(Status.CRC_MISMATCH.value)

    assert pending.success is False
    assert pending.failure_status == Status.CRC_MISMATCH.value
    assert pending.completion.is_set()


def test_pending_command_double_set_event() -> None:
    """Cover that completion event is set only once."""
    pending = PendingCommand(command_id=0x01)

    # First mark
    pending.mark_success()
    assert pending.completion.is_set()

    # Second mark - event already set, should not crash
    pending.mark_failure(Status.TIMEOUT.value)

    # Completion should still be set
    assert pending.completion.is_set()
