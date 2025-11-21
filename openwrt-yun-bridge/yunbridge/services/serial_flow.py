"""Serial flow control for queued MCU commands."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional, Set

from yunbridge.rpc.protocol import Command, Status

SendFrameCallable = Callable[[int, bytes], Awaitable[bool]]

REQUEST_RESPONSE_MAP: Dict[int, Set[int]] = {
    Command.CMD_LINK_RESET.value: {Command.CMD_LINK_RESET_RESP.value},
    Command.CMD_LINK_SYNC.value: {Command.CMD_LINK_SYNC_RESP.value},
    Command.CMD_GET_VERSION.value: {Command.CMD_GET_VERSION_RESP.value},
    Command.CMD_GET_FREE_MEMORY.value: {
        Command.CMD_GET_FREE_MEMORY_RESP.value
    },
    Command.CMD_DIGITAL_READ.value: {Command.CMD_DIGITAL_READ_RESP.value},
    Command.CMD_ANALOG_READ.value: {Command.CMD_ANALOG_READ_RESP.value},
    Command.CMD_DATASTORE_GET.value: {Command.CMD_DATASTORE_GET_RESP.value},
}

ACK_ONLY_COMMANDS: Set[int] = {
    Command.CMD_SET_PIN_MODE.value,
    Command.CMD_DIGITAL_WRITE.value,
    Command.CMD_ANALOG_WRITE.value,
    Command.CMD_CONSOLE_WRITE.value,
    Command.CMD_DATASTORE_PUT.value,
}

RESPONSE_TO_REQUEST: Dict[int, int] = {
    response: request
    for request, responses in REQUEST_RESPONSE_MAP.items()
    for response in responses
}

FAILURE_STATUS_CODES: Set[int] = {
    Status.ERROR.value,
    Status.CMD_UNKNOWN.value,
    Status.MALFORMED.value,
    Status.CRC_MISMATCH.value,
    Status.TIMEOUT.value,
    Status.NOT_IMPLEMENTED.value,
}

SUCCESS_STATUS_CODES: Set[int] = {Status.OK.value}

MIN_ACK_TIMEOUT = 0.05


def _empty_int_set() -> Set[int]:
    return set()


def _expected_responses_for(command_id: int) -> Set[int]:
    responses = REQUEST_RESPONSE_MAP.get(command_id)
    if responses is None:
        return _empty_int_set()
    return set(responses)


def _status_name(code: Optional[int]) -> str:
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
    expected_responses: Set[int] = field(default_factory=_empty_int_set)
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    attempts: int = 0
    success: Optional[bool] = None
    failure_status: Optional[int] = None
    ack_received: bool = False

    def mark_success(self) -> None:
        self.success = True
        if not self.completion.is_set():
            self.completion.set()

    def mark_failure(self, status: Optional[int]) -> None:
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
        metrics_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._ack_timeout = max(ack_timeout, MIN_ACK_TIMEOUT)
        self._response_timeout = max(response_timeout, self._ack_timeout)
        self._max_attempts = max(1, max_attempts)
        self._logger = logger
        self._sender: Optional[SendFrameCallable] = None
        self._condition = asyncio.Condition()
        self._current: Optional[PendingCommand] = None
        self._metrics_callback = metrics_callback

    def set_sender(self, sender: SendFrameCallable) -> None:
        self._sender = sender

    def set_metrics_callback(
        self, callback: Optional[Callable[[str], None]]
    ) -> None:
        self._metrics_callback = callback

    async def reset(self) -> None:
        async with self._condition:
            if self._current and not self._current.completion.is_set():
                self._logger.debug(
                    "Abandoning pending command 0x%02X due to link reset",
                    self._current.command_id,
                )
                self._current.mark_failure(Status.TIMEOUT.value)
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
            expected_responses=_expected_responses_for(command_id),
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
            if pending.expected_responses:
                pending.ack_received = True
                return
            pending.mark_success()
            return

        if command_id in RESPONSE_TO_REQUEST:
            request_id = RESPONSE_TO_REQUEST[command_id]
            if request_id == pending.command_id:
                pending.mark_success()
            return

        if command_id in FAILURE_STATUS_CODES:
            pending.mark_failure(command_id)
            return

        if (
            command_id in SUCCESS_STATUS_CODES
            and not pending.expected_responses
        ):
            pending.mark_success()

    def _should_track(self, command_id: int) -> bool:
        return (
            command_id in REQUEST_RESPONSE_MAP
            or command_id in ACK_ONLY_COMMANDS
        )

    async def _execute_with_retries(
        self,
        pending: PendingCommand,
        payload: bytes,
        sender: SendFrameCallable,
    ) -> bool:
        while pending.attempts < self._max_attempts:
            pending.attempts += 1
            send_ok = await sender(pending.command_id, payload)
            if not send_ok:
                self._logger.error(
                    "Serial write failed for command 0x%02X",
                    pending.command_id,
                )
                pending.mark_failure(None)
                break

            self._emit_metric("sent")

            timeout_requires_retry = False
            ack_phase = True
            while True:
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
                except asyncio.TimeoutError:
                    if pending.completion.is_set():
                        break
                    if ack_phase and pending.ack_received:
                        ack_phase = False
                        continue
                    if pending.attempts >= self._max_attempts:
                        self._logger.error(
                            "Timeout waiting for MCU response to 0x%02X",
                            pending.command_id,
                        )
                        pending.mark_failure(Status.TIMEOUT.value)
                        break
                    self._logger.warning(
                        "Timeout waiting for MCU response to 0x%02X "
                        "(attempt %d/%d)",
                        pending.command_id,
                        pending.attempts,
                        self._max_attempts,
                    )
                    timeout_requires_retry = True
                    break

            if pending.success:
                break

            if timeout_requires_retry:
                pending.ack_received = False
                pending.completion.clear()
                self._emit_metric("retry")
                continue

            status_name = _status_name(pending.failure_status)
            self._logger.warning(
                "MCU rejected command 0x%02X with status %s",
                pending.command_id,
                status_name,
            )
            break

        if pending.success:
            self._emit_metric("ack")
            return True

        self._emit_metric("failure")
        return False
