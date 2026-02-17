"""Tests for daemon supervisor logic (_supervise_task and callbacks)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import tenacity
from mcubridge.daemon import BridgeDaemon


class _TestException(Exception):
    """A normal exception that can be retried."""


def test_supervisor_callbacks_before_sleep_logs_error() -> None:
    """Test _SupervisorCallbacks.before_sleep logs retry info."""
    log = MagicMock(spec=logging.Logger)

    # Access the inner class through BridgeDaemon
    callbacks = BridgeDaemon._SupervisorCallbacks("test-task", log, None)

    # Create a mock RetryCallState
    retry_state = MagicMock(spec=tenacity.RetryCallState)
    retry_state.outcome = MagicMock()
    retry_state.outcome.exception.return_value = _TestException("Test failure")
    retry_state.next_action = MagicMock()
    retry_state.next_action.sleep = 2.5

    callbacks.before_sleep(retry_state)

    log.error.assert_called_once()
    # log.error uses % formatting: "task failed (exc); restarting in Xs"
    call_args = log.error.call_args[0]
    assert "test-task" in call_args  # task name is in the args tuple


def test_supervisor_callbacks_after_retry_with_state() -> None:
    """Test _SupervisorCallbacks.after_retry records failure in state."""
    log = MagicMock(spec=logging.Logger)
    state = MagicMock()

    callbacks = BridgeDaemon._SupervisorCallbacks("test-task", log, state)

    # Create a mock RetryCallState with exception
    retry_state = MagicMock(spec=tenacity.RetryCallState)
    retry_state.outcome = MagicMock()
    retry_state.outcome.exception.return_value = _TestException("Failure")
    retry_state.next_action = MagicMock()
    retry_state.next_action.sleep = 1.0

    callbacks.after_retry(retry_state)

    state.record_supervisor_failure.assert_called_once()


def test_supervisor_callbacks_after_retry_last_attempt() -> None:
    """Test _SupervisorCallbacks.after_retry handles last attempt (no next_action)."""
    log = MagicMock(spec=logging.Logger)
    state = MagicMock()

    callbacks = BridgeDaemon._SupervisorCallbacks("test-task", log, state)

    # Create a mock RetryCallState for last attempt (next_action is None)
    retry_state = MagicMock(spec=tenacity.RetryCallState)
    retry_state.outcome = MagicMock()
    retry_state.outcome.exception.return_value = _TestException("Final failure")
    retry_state.next_action = None  # Last attempt

    callbacks.after_retry(retry_state)

    # Should mark as fatal
    state.record_supervisor_failure.assert_called_once()
    call_kwargs = state.record_supervisor_failure.call_args_list[0]
    assert call_kwargs[1].get("fatal") is True


def test_supervisor_callbacks_with_none_state() -> None:
    """Test _SupervisorCallbacks handles None state gracefully."""
    log = MagicMock(spec=logging.Logger)

    callbacks = BridgeDaemon._SupervisorCallbacks("test-task", log, None)

    retry_state = MagicMock(spec=tenacity.RetryCallState)
    retry_state.outcome = MagicMock()
    retry_state.outcome.exception.return_value = _TestException("Failure")
    retry_state.next_action = MagicMock()
    retry_state.next_action.sleep = 1.0

    # Should not raise even with None state
    callbacks.after_retry(retry_state)


def test_supervisor_callbacks_no_exception_outcome() -> None:
    """Test _SupervisorCallbacks handles no exception outcome."""
    log = MagicMock(spec=logging.Logger)
    state = MagicMock()

    callbacks = BridgeDaemon._SupervisorCallbacks("test-task", log, state)

    # Create a mock RetryCallState with no exception (outcome is None)
    retry_state = MagicMock(spec=tenacity.RetryCallState)
    retry_state.outcome = None
    retry_state.next_action = MagicMock()
    retry_state.next_action.sleep = 1.0

    # Should handle gracefully without calling record_supervisor_failure
    callbacks.before_sleep(retry_state)
    callbacks.after_retry(retry_state)

    # No exception means after_retry skips recording
    state.record_supervisor_failure.assert_not_called()


def test_supervisor_callbacks_before_sleep_no_next_action() -> None:
    """Test _SupervisorCallbacks.before_sleep when next_action is None."""
    log = MagicMock(spec=logging.Logger)

    callbacks = BridgeDaemon._SupervisorCallbacks("test-task", log, None)

    retry_state = MagicMock(spec=tenacity.RetryCallState)
    retry_state.outcome = MagicMock()
    retry_state.outcome.exception.return_value = _TestException("Error")
    retry_state.next_action = None  # No next action

    callbacks.before_sleep(retry_state)

    log.error.assert_called_once()
