"""Serial flow control for queued MCU commands."""

from __future__ import annotations

import asyncio
import logging
import time
import msgspec
from typing import Any, cast
from collections.abc import Awaitable, Callable

import tenacity

from mcubridge.config.const import (
    SERIAL_FAILURE_STATUS_CODES,
    SERIAL_MIN_ACK_TIMEOUT,
    SERIAL_SUCCESS_STATUS_CODES,
)
from mcubridge.protocol.protocol import ACK_ONLY_COMMANDS, RESPONSE_ONLY_COMMANDS
from mcubridge.protocol.contracts import (
    expected_responses,
    response_to_request,
)
from mcubridge.protocol.protocol import Status
from mcubridge.protocol import rle, protocol

SendFrameCallable = Callable[[int, bytes], Awaitable[bool]]


def _set_factory() -> set[int]:
    return set()


def _event_factory() -> asyncio.Event:
    return asyncio.Event()


class PendingCommand(msgspec.Struct):
    """Book-keeping for a tracked command in flight."""

    command_id: int
    expected_resp_ids: set[int] = msgspec.field(default_factory=_set_factory)
    completion: asyncio.Event = msgspec.field(default_factory=_event_factory)
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
        self._pipeline_observer: Callable[[dict[str, Any]], None] | None = None

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

    def set_metrics_callback(self, callback: Callable[[str], None] | None) -> None:
        self._metrics_callback = callback

    def set_pipeline_observer(self, observer: Callable[[dict[str, Any]], None] | None) -> None:
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

        final_cmd = command_id
        final_payload = payload

        # RLE Compression
        if payload and rle.should_compress(payload):
            try:
                compressed = rle.encode(payload)
                if len(compressed) < len(payload):
                    final_cmd |= protocol.CMD_FLAG_COMPRESSED
                    final_payload = compressed
            except (ValueError, TypeError, OverflowError) as e:
                self._logger.warning("Compression failed for command 0x%02X: %s", command_id, e)

        if not self._should_track(command_id):
            return await sender(final_cmd, final_payload)

        pending = PendingCommand(
            command_id=command_id,
            expected_resp_ids=set(expected_responses(command_id)),
        )

        async with self._condition:
            await self._condition.wait_for(self._is_idle)
            self._current = pending

        try:
            return await self._execute_with_retries(pending, final_payload, sender, final_cmd)
        finally:
            async with self._condition:
                if self._current is pending:
                    self._current = None
                    self._condition.notify_all()

    def _is_idle(self) -> bool:
        return self._current is None

    def _emit_metric(self, event: str) -> None:
        if self._metrics_callback is None:
            return
        self._metrics_callback(event)

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
        self._pipeline_observer(payload)

    def on_frame_received(self, command_id: int, payload: bytes) -> None:
        pending = self._current
        if pending is None:
            return

        if command_id == Status.ACK.value:
            ack_target = pending.command_id
            if len(payload) >= 2:
                ack_target = cast(Any, protocol.UINT16_STRUCT).parse(payload[:2])
            if ack_target != pending.command_id:
                return
            if not pending.ack_received:
                pending.ack_received = True
                self._notify_pipeline("ack", pending)
            if pending.expected_resp_ids:
                return
            pending.mark_success()
            return

        request_id = response_to_request(command_id)
        if request_id is not None:
            if request_id == pending.command_id:
                pending.mark_success()
            return

        if command_id in SERIAL_FAILURE_STATUS_CODES:
            # MCU status frames are not reliably correlated to the in-flight
            # command across firmware versions. In particular, some versions
            # emit human-readable reasons like "serial_rx_overflow".
            #
            # To avoid aborting unrelated commands (especially during early
            # handshake), only treat a failure status as "for this command"
            # when either:
            #   - the payload is empty (legacy behavior: unconditional reject)
            #   - the payload starts with the pending command id (big-endian u16)
            #
            # Otherwise, ignore the status for flow-control purposes and let
            # ack/response timeouts drive retries.
            if not payload:
                pending.mark_failure(command_id)
                return

            if len(payload) >= 2:
                target = cast(Any, protocol.UINT16_STRUCT).parse(payload[:2])
                if target == pending.command_id:
                    pending.mark_failure(command_id)
                    return

            if all(32 <= byte < 127 for byte in payload):
                return

            pending.mark_failure(command_id)
            return

        if command_id in SERIAL_SUCCESS_STATUS_CODES and not pending.expected_resp_ids:
            pending.mark_success()

    def _should_track(self, command_id: int) -> bool:
        return bool(expected_responses(command_id)) or command_id in ACK_ONLY_COMMANDS

    def _build_retryer(self) -> tenacity.AsyncRetrying:
        """Build tenacity retryer with configured limits."""
        return tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(self._max_attempts),
            retry=tenacity.retry_if_exception_type(self._RetryableSerialError),
            before_sleep=self._on_retry_sleep,
            reraise=True,
        )

    def _on_retry_sleep(self, retry_state: tenacity.RetryCallState) -> None:
        """Callback invoked before each retry sleep."""
        self._emit_metric("retry")
        self._logger.warning(
            "Timeout waiting for MCU response (attempt %d/%d)",
            retry_state.attempt_number,
            self._max_attempts,
        )

    async def _single_attempt(
        self,
        pending: PendingCommand,
        payload: bytes,
        sender: SendFrameCallable,
        cmd_to_send: int,
    ) -> bool:
        """Execute a single send attempt. Raises on retryable/fatal errors."""
        pending.attempts = (pending.attempts or 0) + 1
        self._notify_pipeline("start", pending)
        self._reset_pending_state(pending)
        await self._send_and_wait(pending, payload, sender, cmd_to_send)
        self._emit_metric("ack")
        self._notify_pipeline("success", pending)
        return True

    async def _execute_with_retries(
        self,
        pending: PendingCommand,
        payload: bytes,
        sender: SendFrameCallable,
        actual_cmd_id: int | None = None,
    ) -> bool:
        cmd_to_send = actual_cmd_id if actual_cmd_id is not None else pending.command_id

        try:
            retryer = self._build_retryer()
            return await retryer(self._single_attempt, pending, payload, sender, cmd_to_send)
        except self._RetryableSerialError:
            pending.mark_failure(Status.TIMEOUT.value)
            self._notify_pipeline("failure", pending, status=Status.TIMEOUT.value)
        except self._FatalSerialError as exc:
            pending.mark_failure(exc.status)
            self._notify_pipeline("failure", pending, status=exc.status)

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
        actual_cmd_id: int,
    ) -> None:
        send_ok = await sender(actual_cmd_id, payload)
        if not send_ok:
            self._logger.error(
                "Serial write failed for command 0x%02X",
                pending.command_id,
            )
            pending.mark_failure(None)
            raise self._FatalSerialError(None)

        self._emit_metric("sent")

        # Commands in RESPONSE_ONLY_COMMANDS don't expect ACK, skip ack phase
        ack_phase = pending.command_id not in RESPONSE_ONLY_COMMANDS
        while True:
            if ack_phase and pending.ack_received:
                ack_phase = False
            timeout = self._ack_timeout if ack_phase else self._response_timeout
            try:
                async with asyncio.timeout(timeout):
                    await pending.completion.wait()
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

        if pending.failure_status is not None:
            try:
                status_name = Status(pending.failure_status).name
            except ValueError:
                status_name = f"0x{pending.failure_status:02X}"

            self._logger.warning(
                "MCU rejected command 0x%02X with status %s",
                pending.command_id,
                status_name,
            )
            raise self._FatalSerialError(pending.failure_status)

        raise self._RetryableSerialError()
