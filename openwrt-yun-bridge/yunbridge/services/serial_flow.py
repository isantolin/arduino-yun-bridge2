"""Serial flow control for queued MCU commands."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Awaitable, Callable

from yunbridge.const import (
    SERIAL_ACK_ONLY_COMMANDS,
    SERIAL_FAILURE_STATUS_CODES,
    SERIAL_MIN_ACK_TIMEOUT,
    SERIAL_SUCCESS_STATUS_CODES,
)
from yunbridge.rpc.contracts import expected_responses, response_to_request
from yunbridge.rpc.protocol import Status

SendFrameCallable = Callable[[int, bytes], Awaitable[bool]]


def _empty_int_set() -> set[int]:
    return set()


def _status_name(code: int | None) -> str:
    if code is None:
        return "unknown"
    try:
        return Status(code).name
    except ValueError:
        return f"0x{code:02X}"


@dataclass
class PendingCommand:
    """Book-keeping for a tracked command in flight."""

    command_id: int
    expected_responses: set[int] = field(default_factory=_empty_int_set)
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    attempts: int = 0
    success: bool | None = None
    failure_status: int | None = None
    ack_received: bool = False

    def mark_success(self) -> None:
        self.success = True
        if not self.completion.is_set():
            self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        self.failure_status = status
        if not self.completion.is_set():
            self.completion.set()


class SerialFlowController:
    """Sequentialises MCU commands and retries on missing responses."""

    def __init__(
        self,
        *,
        ack_timeout: float,
        response_timeout: float,
        max_attempts: int,
        logger: logging.Logger,
        metrics_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._ack_timeout = max(ack_timeout, SERIAL_MIN_ACK_TIMEOUT)
        self._response_timeout = max(response_timeout, self._ack_timeout)
        self._max_attempts = max(1, max_attempts)
        self._logger = logger
        self._sender: SendFrameCallable | None = None
        self._condition = asyncio.Condition()
        self._current: PendingCommand | None = None
        self._metrics_callback = metrics_callback
        self._pipeline_observer: Callable[[dict[str, Any]], None] | None = (
            None
        )

    #  --- Tenacity Helpers ---
    class _RetryableSerialError(Exception):
        """Marker exception to request another send attempt."""

        pass

    class _FatalSerialError(Exception):
        """Raised when a frame should not be retried."""

        def __init__(self, status: int | None) -> None:
            super().__init__(status)
            self.status = status

    def set_sender(self, sender: SendFrameCallable) -> None:
        self._sender = sender

    def set_metrics_callback(
        self, callback: Callable[[str], None] | None
    ) -> None:
        self._metrics_callback = callback

    def set_pipeline_observer(
        self, observer: Callable[[dict[str, Any]], None] | None
    ) -> None:
        self._pipeline_observer = observer

    async def reset(self) -> None:
        async with self._condition:
            if self._current and not self._current.completion.is_set():
                self._logger.debug(
                    "Abandoning pending command 0x%02X due to link reset",
                    self._current.command_id,
                )
                self._current.mark_failure(Status.TIMEOUT.value)
                self._notify_pipeline(
                    "abandoned",
                    self._current,
                    status=Status.TIMEOUT.value,
                )
            self._current = None
            self._condition.notify_all()

    async def send(self, command_id: int, payload: bytes) -> bool:
        sender = self._sender
        if sender is None:
            self._logger.error(
                "Serial writer unavailable; dropping frame 0x%02X",
                command_id,
            )
            return False

        if not self._should_track(command_id):
            return await sender(command_id, payload)

        pending = PendingCommand(
            command_id=command_id,
            expected_responses=set(expected_responses(command_id)),
        )

        async with self._condition:
            await self._condition.wait_for(lambda: self._current is None)
            self._current = pending

        try:
            return await self._execute_with_retries(pending, payload, sender)
        finally:
            async with self._condition:
                if self._current is pending:
                    self._current = None
                    self._condition.notify_all()

    def _emit_metric(self, event: str) -> None:
        if self._metrics_callback is None:
            return
        try:
            self._metrics_callback(event)
        except Exception:  # pragma: no cover - defensive guard
            self._logger.exception("Serial metrics callback failed")

    def _notify_pipeline(
        self,
        event: str,
        pending: PendingCommand,
        *,
        status: int | None = None,
    ) -> None:
        if self._pipeline_observer is None:
            return
        payload = {
            "event": event,
            "command_id": pending.command_id,
            "attempt": max(1, pending.attempts or 1),
            "ack_received": pending.ack_received,
            "status": status,
            "timestamp": time.time(),
        }
        try:
            self._pipeline_observer(payload)
        except Exception:  # pragma: no cover - defensive guard
            self._logger.exception("Serial pipeline observer failed")

    def on_frame_received(self, command_id: int, payload: bytes) -> None:
        pending = self._current
        if pending is None:
            return

        if command_id == Status.ACK.value:
            ack_target = pending.command_id
            if len(payload) >= 2:
                ack_target = int.from_bytes(payload[:2], "big")
            if ack_target != pending.command_id:
                return
            if not pending.ack_received:
                pending.ack_received = True
                self._notify_pipeline("ack", pending)
            if pending.expected_responses:
                return
            pending.mark_success()
            return

        request_id = response_to_request(command_id)
        if request_id is not None:
            if request_id == pending.command_id:
                pending.mark_success()
            return

        if command_id in SERIAL_FAILURE_STATUS_CODES:
            pending.mark_failure(command_id)
            return

        if (
            command_id in SERIAL_SUCCESS_STATUS_CODES
            and not pending.expected_responses
        ):
            pending.mark_success()

    def _should_track(self, command_id: int) -> bool:
        return (
            bool(expected_responses(command_id))
            or command_id in SERIAL_ACK_ONLY_COMMANDS
        )

    async def _execute_with_retries(
        self,
        pending: PendingCommand,
        payload: bytes,
        sender: SendFrameCallable,
    ) -> bool:
        for attempt_num in range(1, self._max_attempts + 1):
            try:
                pending.attempts = attempt_num
                self._notify_pipeline("start", pending)
                self._reset_pending_state(pending)
                await self._send_and_wait(pending, payload, sender)
                self._emit_metric("ack")
                self._notify_pipeline("success", pending)
                return True
            except self._RetryableSerialError:
                if attempt_num < self._max_attempts:
                    self._emit_metric("retry")
                    self._logger.warning(
                        "Timeout waiting for MCU response to 0x%02X (attempt %d/%d)",
                        pending.command_id,
                        attempt_num,
                        self._max_attempts,
                    )
                    continue
                else:
                    pending.mark_failure(Status.TIMEOUT.value)
                    self._notify_pipeline(
                        "failure",
                        pending,
                        status=Status.TIMEOUT.value,
                    )
            except self._FatalSerialError as exc:
                pending.mark_failure(exc.status)
                self._notify_pipeline("failure", pending, status=exc.status)
                break

        self._emit_metric("failure")
        return False

    def _reset_pending_state(self, pending: PendingCommand) -> None:
        pending.completion.clear()
        pending.ack_received = False
        pending.success = None
        pending.failure_status = None

    async def _send_and_wait(
        self,
        pending: PendingCommand,
        payload: bytes,
        sender: SendFrameCallable,
    ) -> None:
        send_ok = await sender(pending.command_id, payload)
        if not send_ok:
            self._logger.error(
                "Serial write failed for command 0x%02X",
                pending.command_id,
            )
            pending.mark_failure(None)
            raise self._FatalSerialError(None)

        self._emit_metric("sent")

        ack_phase = True
        while True:
            if ack_phase and pending.ack_received:
                ack_phase = False
            timeout = (
                self._ack_timeout
                if ack_phase
                else self._response_timeout
            )
            try:
                await asyncio.wait_for(
                    pending.completion.wait(),
                    timeout=timeout,
                )
                break
            except TimeoutError:
                if pending.completion.is_set():
                    break
                if ack_phase and pending.ack_received:
                    ack_phase = False
                    continue
                raise self._RetryableSerialError()

        if pending.success:
            return

        status_name = _status_name(pending.failure_status)
        if pending.failure_status is not None:
            self._logger.warning(
                "MCU rejected command 0x%02X with status %s",
                pending.command_id,
                status_name,
            )
            raise self._FatalSerialError(pending.failure_status)

        raise self._RetryableSerialError()
