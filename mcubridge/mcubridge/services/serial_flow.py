"""Serial flow control for queued MCU commands."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import tenacity

from mcubridge.config.const import (
    SERIAL_FAILURE_STATUS_CODES,
    SERIAL_MIN_ACK_TIMEOUT,
    SERIAL_SUCCESS_STATUS_CODES,
)
from mcubridge.protocol.protocol import (
    ACK_ONLY_COMMANDS,
    RESPONSE_ONLY_COMMANDS,
    Status,
    expected_responses,
    response_to_request,
)
from mcubridge.protocol.structures import AckPacket, PendingCommand

SendFrameCallable = Callable[[int, bytes], Awaitable[bool]]


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

    @staticmethod
    def chunk_payload(payload: bytes, chunk_size: int) -> list[bytes]:
        """Split a large payload into chunks that fit within MCU buffers (SIL-2)."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if not payload:
            return []

        import itertools

        return [bytes(chunk) for chunk in itertools.batched(payload, chunk_size)]

    def set_metrics_callback(self, callback: Callable[[str], None] | None) -> None:
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

    async def send(self, command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
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
            expected_resp_ids=set(expected_responses(command_id)),
        )

        async with self._condition:
            await self._condition.wait_for(lambda: self._current is None)
            self._current = pending

        try:
            return await self._execute_with_retries(
                pending, payload, sender, command_id
            )
        finally:
            async with self._condition:
                if self._current is pending:
                    self._current = None
                    self._condition.notify_all()

    async def acknowledge(
        self,
        command_id: int,
        seq_id: int,
        *,
        status: Status = Status.ACK,
    ) -> None:
        """Send an acknowledgement frame to the MCU (SIL-2)."""
        sender = self._sender
        if not sender:
            self._logger.error(
                "Serial writer unavailable; cannot acknowledge frame 0x%02X",
                command_id,
            )
            return

        payload = AckPacket(command_id=command_id).encode()
        try:
            await sender(status.value, payload)
        except (OSError, RuntimeError, ValueError) as exc:
            self._logger.warning(
                "Failed to enqueue status %s for command 0x%02X: %s",
                status.name,
                command_id,
                exc,
            )

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

    def on_frame_received(
        self, command_id: int, sequence_id: int, payload: bytes
    ) -> None:
        pending = self._current
        if pending is None:
            return

        if command_id == Status.ACK.value:
            ack_target = pending.command_id
            if payload:
                try:
                    ack_target = AckPacket.decode(payload).command_id
                except ValueError:
                    pass
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
            # MCU status frames correlation logic — try AckPacket protobuf first
            if not payload:
                should_reject = True
            else:
                try:
                    should_reject = (
                        AckPacket.decode(payload).command_id == pending.command_id
                    )
                except ValueError:
                    # Non-protobuf (human-readable string) → reject only if binary
                    should_reject = not all(32 <= byte < 127 for byte in payload)

            if should_reject:
                pending.mark_failure(command_id)
            return

        if command_id in SERIAL_SUCCESS_STATUS_CODES and not pending.expected_resp_ids:
            pending.mark_success()

    def _should_track(self, command_id: int) -> bool:
        return bool(expected_responses(command_id)) or command_id in ACK_ONLY_COMMANDS

    def _build_retryer(self) -> tenacity.AsyncRetrying:
        """Build tenacity retryer with configured limits."""
        from mcubridge.config.const import (
            SERIAL_HANDSHAKE_BACKOFF_BASE,
            SERIAL_HANDSHAKE_BACKOFF_MAX,
        )

        return tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(self._max_attempts),
            wait=tenacity.wait_exponential(
                multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE,
                max=SERIAL_HANDSHAKE_BACKOFF_MAX,
            ),
            retry=tenacity.retry_if_exception_type(self._RetryableSerialError),
            before_sleep=self._on_retry_sleep,
            reraise=True,
        )

    async def _single_attempt(
        self,
        pending: PendingCommand,
        payload: bytes,
        sender: SendFrameCallable,
        cmd_to_send: int,
    ) -> bool:
        """Execute a single send attempt. Raises on retryable/fatal errors."""
        # [SIL-2] Extract attempt number from tenacity context if available
        # otherwise fallback to manual tracking.
        try:
            # [SIL-2] Manual attempt tracking for determinism
            pending.attempts = (pending.attempts or 0) + 1
        except AttributeError:
            pending.attempts = (pending.attempts or 0) + 1

        self._notify_pipeline("start", pending)
        self._reset_pending_state(pending)
        await self._send_and_wait(pending, payload, sender, cmd_to_send)
        self._emit_metric("ack")
        self._notify_pipeline("success", pending)
        return True

    async def acknowledge(
        self,
        command_id: int,
        seq_id: int,
        status: int = 56,  # Status.ACK.value
    ) -> bool:
        """Send a low-level status response (ACK/Status) to the MCU (SIL-2)."""
        if self._sender is None:
            self._logger.error("Serial sender not registered; cannot send ACK")
            return False

        from ..protocol.structures import AckPacket

        payload = AckPacket(command_id=command_id).encode()

        try:
            # ACKs are fire-and-forget at the flow level (no ACK-of-ACK)
            return await self._sender(status, payload, seq_id)
        except (OSError, RuntimeError, ValueError) as exc:
            self._logger.warning(
                "Failed to emit status 0x%02X for command 0x%02X: %s",
                status,
                command_id,
                exc,
            )
            return False

    async def negotiate_baudrate(self, target_baud: int) -> bool:
        """Execute baudrate negotiation sequence with the MCU (SIL-2).
        Sends CMD_SET_BAUDRATE and waits for CMD_SET_BAUDRATE_RESP.
        """
        if self._sender is None:
            return False

        from ..protocol.structures import SetBaudratePacket

        payload = SetBaudratePacket(baudrate=target_baud).encode()

        # [SIL-2] Use a dedicated pending state for negotiation to avoid
        # interference with standard RPC flows.
        pending = PendingCommand(
            command_id=Command.CMD_SET_BAUDRATE.value,
            expected_resp_ids={Command.CMD_SET_BAUDRATE_RESP.value},
        )

        async with self._condition:
            await self._condition.wait_for(lambda: self._current is None)
            self._current = pending

        try:
            # Send command (fire-and-forget at this baudrate)
            await self._sender(Command.CMD_SET_BAUDRATE.value, payload, 0)

            # Wait for response with specific timeout.
            # MCU switches baudrate immediately after receiving command,
            # so the response will arrive on the NEW baudrate.
            try:
                async with asyncio.timeout(self._response_timeout):
                    await pending.future
                return True
            except asyncio.TimeoutError:
                return False
        finally:
            async with self._condition:
                if self._current is pending:
                    self._current = None

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
            return await retryer(
                self._single_attempt, pending, payload, sender, cmd_to_send
            )
        except self._RetryableSerialError:
            pending.mark_failure(Status.TIMEOUT.value)
            self._notify_pipeline("failure", pending, status=Status.TIMEOUT.value)
        except self._FatalSerialError as exc:
            pending.mark_failure(exc.status)
            self._notify_pipeline("failure", pending, status=exc.status)

        self._emit_metric("failure")
        return False

    def _on_retry_sleep(self, retry_state: tenacity.RetryCallState) -> None:
        self._emit_metric("retry")
        tenacity.before_sleep_log(self._logger, logging.WARNING)(retry_state)

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
        if not await sender(actual_cmd_id, payload):
            self._logger.error(
                "Serial write failed for command 0x%02X", pending.command_id
            )
            pending.mark_failure(None)
            raise self._FatalSerialError(None)

        self._emit_metric("sent")

        # [SIL-2] Precise wait logic with library-backed timeouts
        expect_ack = pending.command_id not in RESPONSE_ONLY_COMMANDS

        try:
            async with asyncio.timeout(self._response_timeout):
                # 1. Wait for ACK if required
                if expect_ack and not pending.ack_received:
                    await pending.completion.wait()
                    # If it was a success mark (direct response), we're done.
                    # Otherwise, it might just be the ACK.
                    if pending.success:
                        return

                # 2. Wait for full completion (Response) if not already success
                if not pending.success:
                    await pending.completion.wait()
        except asyncio.TimeoutError:
            raise self._RetryableSerialError()

        if pending.success:
            return

        if pending.failure_status is not None:
            # [SIL-2] Direct library mapping for status labels
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
