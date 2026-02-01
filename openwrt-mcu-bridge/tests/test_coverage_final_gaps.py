"""Tests targeting coverage gaps in task_supervisor, serial_flow, and payloads."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from mcubridge.services.payloads import (
    PayloadValidationError,
    ShellCommandPayload,
    ShellPidPayload,
)
from mcubridge.services.serial_flow import SerialFlowController, PendingCommand
from mcubridge.services.task_supervisor import (
    SupervisedTaskSpec,
    supervise_task,
    _SupervisorRetryState,
)
from mcubridge.rpc.protocol import Status


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
# Task Supervisor Tests
# ============================================================================


def test_supervisor_retry_state_not_healthy() -> None:
    """Cover retry state when not healthy (no start time)."""
    state = _SupervisorRetryState(
        name="test",
        log=logging.getLogger("test"),
        state=None,
        window=5.0,
    )

    assert state.is_healthy_runtime() is False


def test_supervisor_retry_state_healthy() -> None:
    """Cover retry state when healthy."""
    state = _SupervisorRetryState(
        name="test",
        log=logging.getLogger("test"),
        state=None,
        window=0.01,  # Very short window
    )

    state.mark_started()
    import time
    time.sleep(0.02)

    assert state.is_healthy_runtime() is True


def test_supervisor_retry_state_before_sleep() -> None:
    """Cover before_sleep callback."""
    state = _SupervisorRetryState(
        name="test",
        log=MagicMock(),
        state=None,
        window=5.0,
    )

    mock_retry_state = MagicMock()
    mock_retry_state.outcome = MagicMock()
    mock_retry_state.outcome.exception.return_value = RuntimeError("test")
    mock_retry_state.next_action = MagicMock()
    mock_retry_state.next_action.sleep = 1.5

    state.before_sleep(mock_retry_state)
    state.log.error.assert_called()


def test_supervisor_retry_state_after_with_state() -> None:
    """Cover after callback with RuntimeState."""
    mock_runtime = MagicMock()
    helper = _SupervisorRetryState(
        name="test",
        log=logging.getLogger("test"),
        state=mock_runtime,
        window=5.0,
    )

    mock_retry_state = MagicMock()
    mock_retry_state.outcome = MagicMock()
    mock_retry_state.outcome.exception.return_value = RuntimeError("test")
    mock_retry_state.next_action = MagicMock()
    mock_retry_state.next_action.sleep = 2.0

    helper.after(mock_retry_state)

    mock_runtime.record_supervisor_failure.assert_called_once()


def test_supervisor_retry_state_after_no_exception() -> None:
    """Cover after callback when no exception."""
    mock_runtime = MagicMock()
    helper = _SupervisorRetryState(
        name="test",
        log=logging.getLogger("test"),
        state=mock_runtime,
        window=5.0,
    )

    mock_retry_state = MagicMock()
    mock_retry_state.outcome = MagicMock()
    mock_retry_state.outcome.exception.return_value = None
    mock_retry_state.next_action = None

    helper.after(mock_retry_state)

    # Should not call record_supervisor_failure
    mock_runtime.record_supervisor_failure.assert_not_called()


@pytest.mark.asyncio
async def test_supervise_task_clean_exit() -> None:
    """Cover task that exits cleanly."""
    call_count = 0

    async def simple_task() -> None:
        nonlocal call_count
        call_count += 1

    mock_state = MagicMock()

    await supervise_task(
        "test_task",
        simple_task,
        state=mock_state,
    )

    assert call_count == 1
    mock_state.mark_supervisor_healthy.assert_called_with("test_task")


@pytest.mark.asyncio
async def test_supervise_task_cancelled() -> None:
    """Cover task supervisor cancellation."""
    async def forever_task() -> None:
        await asyncio.sleep(1000)

    task = asyncio.create_task(
        supervise_task("test_cancel", forever_task)
    )

    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_supervise_task_fatal_exception() -> None:
    """Cover task with fatal exception."""
    class FatalError(Exception):
        pass

    async def fatal_task() -> None:
        raise FatalError("fatal!")

    mock_state = MagicMock()

    with pytest.raises(FatalError):
        await supervise_task(
            "test_fatal",
            fatal_task,
            fatal_exceptions=(FatalError,),
            state=mock_state,
        )

    mock_state.record_supervisor_failure.assert_called()


@pytest.mark.asyncio
async def test_supervise_task_max_restarts() -> None:
    """Cover task that exceeds max restarts."""
    call_count = 0

    async def failing_task() -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("always fails")

    with pytest.raises(RuntimeError):
        await supervise_task(
            "test_max",
            failing_task,
            max_restarts=2,
            min_backoff=0.01,
            max_backoff=0.01,
        )

    assert call_count == 3  # Initial + 2 restarts


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


# ============================================================================
# SupervisedTaskSpec Tests
# ============================================================================


def test_supervised_task_spec_defaults() -> None:
    """Cover SupervisedTaskSpec default values."""
    async def dummy() -> None:
        pass

    spec = SupervisedTaskSpec(name="test", factory=dummy)

    assert spec.name == "test"
    assert spec.fatal_exceptions == ()
    assert spec.max_restarts is None
