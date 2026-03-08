"""Serial flow control for queued MCU commands."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import tenacity

from ..config.const import SERIAL_MIN_ACK_TIMEOUT
from ..config.settings import RuntimeConfig
from ..protocol.contracts import response_to_request
from ..protocol.protocol import ACK_ONLY_COMMANDS, RESPONSE_ONLY_COMMANDS, Status

logger = logging.getLogger("mcubridge.service.serial_flow")


class PendingRequest:
    """Represents a request awaiting MCU acknowledgement or response."""

    def __init__(self, command_id: int, payload: bytes) -> None:
        self.command_id = command_id
        self.payload = payload
        self.ack_received = False
        self.completion = asyncio.Event()
        self.failure_status: int | None = None
        self.start_time = time.monotonic()

    def mark_ack(self) -> None:
        self.ack_received = True

    def mark_success(self) -> None:
        self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.failure_status = status
        self.completion.set()


class SerialFlowController:
    """Coordinates command queuing and explicit MCU acknowledgement flow."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._ack_timeout = max(config.serial_retry_timeout * 5.0, SERIAL_MIN_ACK_TIMEOUT * 2.0)
        self._response_timeout = config.serial_response_timeout * 5.0
        self._retry_attempts = config.serial_retry_attempts
        self._condition = asyncio.Condition()
        self._current: PendingRequest | None = None

    class _RetryableSerialError(Exception):
        """Internal error to trigger tenacity retries."""

    class _FatalSerialError(Exception):
        """Internal error to stop retries (e.g. MCU rejected command)."""

    async def send(self, command_id: int, payload: bytes, sender: Callable[[int, bytes], Awaitable[bool]]) -> bool:
        """Entry point for all non-status MCU commands."""
        logger.debug("SerialFlow: send(0x%02X, len=%d)", command_id, len(payload))

        # [SIL-2] Use numerical command IDs for jump table compatibility.
        final_cmd = command_id
        final_payload = payload

        # Check if this command requires tracking
        if not self._should_track(command_id):
            logger.debug("SerialFlow: command 0x%02X does not require tracking", command_id)
            return await sender(final_cmd, final_payload)

        pending = PendingRequest(command_id, payload)

        async with self._condition:
            if self._current:
                logger.debug("SerialFlow: waiting for current request 0x%02X to finish", self._current.command_id)
            await self._condition.wait_for(lambda: self._current is None)
            self._current = pending

        try:
            logger.debug("SerialFlow: executing request 0x%02X", command_id)
            return await self._execute_with_retries(pending, sender, final_cmd, final_payload)
        finally:
            async with self._condition:
                logger.debug("SerialFlow: finished request 0x%02X", command_id)
                self._current = None
                self._condition.notify_all()

    def on_ack_received(self, command_id: int) -> None:
        """Handle explicit STATUS_ACK from MCU."""
        if self._current and self._current.command_id == command_id:
            logger.debug("SerialFlow: received ACK for 0x%02X", command_id)
            self._current.mark_ack()
        else:
            logger.debug("SerialFlow: received ACK for 0x%02X but no current request matches", command_id)

    def on_frame_received(self, command_id: int, _payload: bytes) -> None:
        """Handle any valid frame from MCU (might resolve a pending request)."""
        if not self._current:
            return

        # Check if this frame is a response to the current request
        request_id = response_to_request(command_id)
        if request_id is not None:
            if request_id == self._current.command_id:
                logger.debug("SerialFlow: received response 0x%02X for request 0x%02X", command_id, request_id)
                self._current.mark_success()

    def on_status_received(self, status: Status) -> None:
        """Handle error statuses that might indicate request failure."""
        if not self._current:
            return

        if status not in (Status.OK, Status.ACK):
            self._current.mark_failure(status.value)

    def _should_track(self, command_id: int) -> bool:
        from ..protocol.contracts import expected_responses
        return bool(expected_responses(command_id)) or command_id in ACK_ONLY_COMMANDS

    async def _execute_with_retries(
        self,
        pending: PendingRequest,
        sender: Callable[[int, bytes], Awaitable[bool]],
        command_id: int,
        payload: bytes,
    ) -> bool:
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(self._retry_attempts + 1),
            wait=tenacity.wait_fixed(0.1),
            retry=tenacity.retry_if_exception_type(self._RetryableSerialError),
            reraise=False,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    await self._single_attempt(pending, sender, command_id, payload)
            return True
        except self._FatalSerialError:
            return False
        except tenacity.RetryError:
            logger.warning(
                "Command 0x%02X failed after %d attempts",
                pending.command_id,
                self._retry_attempts + 1,
            )
            return False

    async def _single_attempt(
        self,
        pending: PendingRequest,
        sender: Callable[[int, bytes], Awaitable[bool]],
        command_id: int,
        payload: bytes,
    ) -> None:
        try:
            await self._send_and_wait(pending, sender, command_id, payload)
        except (self._RetryableSerialError, asyncio.TimeoutError):
            # Reset ephemeral state for retry
            pending.ack_received = False
            raise self._RetryableSerialError()

    async def _send_and_wait(
        self,
        pending: PendingRequest,
        sender: Callable[[int, bytes], Awaitable[bool]],
        command_id: int,
        payload: bytes,
    ) -> None:
        send_ok = await sender(command_id, payload)
        if not send_ok:
            raise self._FatalSerialError(None)

        # Commands in RESPONSE_ONLY_COMMANDS don't expect ACK, skip ack phase
        expect_ack = pending.command_id not in RESPONSE_ONLY_COMMANDS

        try:
            # 1. Wait for ACK (if expected)
            if expect_ack and not pending.ack_received:
                async with asyncio.timeout(self._ack_timeout):
                    while not pending.ack_received and not pending.completion.is_set():
                        await asyncio.sleep(0.01)

            # [FIX] If it's an ACK_ONLY command, we are done once ACK is received.
            if pending.command_id in ACK_ONLY_COMMANDS:
                pending.mark_success()

            # 2. Wait for Response/Completion
            if not pending.completion.is_set():
                async with asyncio.timeout(self._response_timeout):
                    while not pending.completion.is_set():
                        await asyncio.sleep(0.01)

        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for MCU response to 0x%02X", pending.command_id)
            raise

        if pending.failure_status is not None:
            try:
                status_name = Status(pending.failure_status).name
            except ValueError:
                status_name = f"0x{pending.failure_status:02X}"

            logger.warning(
                "MCU rejected command 0x%02X with status %s",
                pending.command_id,
                status_name,
            )
            raise self._FatalSerialError(pending.failure_status)
