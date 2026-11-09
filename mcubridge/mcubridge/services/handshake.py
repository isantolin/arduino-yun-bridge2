"""Handshake and link synchronization management for MCU Bridge.

[SIL-2] All framing, nonce validation, and state transitions are handled
through a strict finite state machine (FSM) using `transitions` and
`tenacity` for deterministic retry policies.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Final, cast

import structlog
import tenacity
from google.protobuf.message import DecodeError as ProtobufDecodeError
from google.protobuf.message import Message as ProtobufMessage
from transitions.extensions.asyncio import AsyncMachine

from ..config.const import (
     HANDSHAKE_FATAL_FAILURE_THRESHOLD,
)
from ..config.settings import RuntimeConfig
from ..protocol import pb, protocol
from ..protocol.protocol import (
    Command,
    HandshakeConfig,
    Status,
)
from ..protocol.structures import (
    HandshakeAckPacket,
    LinkSyncPacket,
)
from ..state.context import RuntimeState
from ..util import derive_serial_Timing

logger = structlog.get_logger("mcubridge.handshake")


class HandshakeState(Enum):
    """[SIL-2] Explicit Finite State Machine states for Link Handshake."""

    UNSYNCHRONIZED = auto()
    RESETTING = auto()
    SENDING_SYNC = auto()
    AWAITING_SYNC_RESP = auto()
    CONFIRMING = auto()
    SYNCHRONIZED = auto()
    FAULT = auto()


@dataclass
class HandshakeStats:
    """Diagnostic counters for handshake operations."""

    attempts: int = 0
    successes: int = 0
    failures: int = 0
    fatal_errors: int = 0
    last_error: str | None = None
    last_sync_unix: float = 0.0
    Synchronization_duration_ms: float = 0.0


@dataclass
class SerialHandshakeManager:
    """Manages MCU reset, link synchronization, and capability discovery."""

    config: RuntimeConfig
    state: RuntimeState
    send_frame: Callable[[int, bytes], Awaitable[Any]]
    enqueue_mqtt: Callable[[Any], Awaitable[None]]
    acknowledge_frame: Callable[
        [int, int],
        Awaitable[None],
    ]
    on_synchronized_callbacks: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    reset_hook: Callable[[float], Awaitable[float]] | None = None
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self._config = self.config
        self._state = self.state
        self._logger = logger
        self.stats = HandshakeStats()

        # Timing parameters derived from baudrate and safety margins
        timing = derive_serial_Timing(
            self._config.serial_baud,
            self._config.serial_safe_baud,
            self._config.serial_retry_timeout,
            self._config.serial_response_timeout,
        )
        self.ack_timeout_ms: int = timing.ack_timeout_ms
        self.response_timeout_ms: int = timing.response_timeout_ms
        self.retry_limit: int = self._config.serial_retry_limit
        self.retry_backoff_base: float = 0.2
        self.retry_backoff_max: float = 2.0
        self.post_reset_delay: float = 2.0
        self.capabilities_delay: float = 2.0

        self._handshake_lock = asyncio.Lock()
        self._active_nonce: bytes | None = None
        self._sync_started_at: float = 0.0
        self._expected_resp_future: asyncio.Future[tuple[int, bytes]] | None = None
        self._consecutive_fatal_failures: int = 0

        # [SIL-2] Formal AsyncMachine declaration
        self.machine = AsyncMachine(
            model=self,
            states=HandshakeState,
            initial=HandshakeState.UNSYNCHRONIZED,
            auto_transitions=False,
            send_event=True,
            queued=True,
        )

        # Transitions definition
        self.machine.add_transition(
            trigger="begin_reset",
            source=[
                HandshakeState.UNSYNCHRONIZED,
                HandshakeState.SYNCHRONIZED,
                HandshakeState.FAULT,
            ],
            dest=HandshakeState.RESETTING,
            before="_on_enter_resetting",
        )
        self.machine.add_transition(
            trigger="start_sync",
            source=HandshakeState.RESETTING,
            dest=HandshakeState.SENDING_SYNC,
        )
        self.machine.add_transition(
            trigger="wait_response",
            source=HandshakeState.SENDING_SYNC,
            dest=HandshakeState.AWAITING_SYNC_RESP,
        )
        self.machine.add_transition(
            trigger="confirm_sync",
            source=HandshakeState.AWAITING_SYNC_RESP,
            dest=HandshakeState.CONFIRMING,
        )
        self.machine.add_transition(
            trigger="complete_sync",
            source=[
                HandshakeState.CONFIRMING,
                HandshakeState.AWAITING_SYNC_RESP,
                HandshakeState.UNSYNCHRONIZED,
                HandshakeState.SYNCHRONIZED,
            ],
            dest=HandshakeState.SYNCHRONIZED,
            after="_on_enter_synchronized",
        )
        self.machine.add_transition(
            trigger="fail_sync",
            source="*",
            dest=HandshakeState.FAULT,
            before="_on_enter_fault",
        )
        self.machine.add_transition(
            trigger="reset_to_unsync",
            source="*",
            dest=HandshakeState.UNSYNCHRONIZED,
        )

    @property
    def is_synchronized(self) -> bool:
        return self.state_fsm == HandshakeState.SYNCHRONIZED

    @property
    def state_fsm(self) -> HandshakeState:
        return cast(HandshakeState, getattr(self, "state"))

    async def _on_enter_resetting(self, event: Any) -> None:
        self._state.mark_transport_connected()
        self._state.link_is_synchronized = False
        self._sync_started_at = self.clock()
        self.stats.attempts += 1

    async def _on_enter_synchronized(self, event: Any) -> None:
        duration = (self.clock() - self._sync_started_at) * 1000.0
        self.stats.Synchronization_duration_ms = duration
        self.stats.successes += 1
        self.stats.last_sync_unix = time.time()
        self._consecutive_fatal_failures = 0
        self._state.mark_synchronized()

        for cb in self.on_synchronized_callbacks:
            try:
                await cb()
            except (RuntimeError, ValueError, TypeError, OSError) as exc:
                self._logger.error(
                    "Error in on_synchronized callback",
                    error=str(exc),
                )

    async def _on_enter_fault(self, event: Any) -> None:
        reason = event.kwargs.get("reason", "unknown")
        self.stats.failures += 1
        self.stats.last_error = str(reason)
        self._state.link_is_synchronized = False
        self._state.handshake_failure_streak += 1

    def prepare_handshake_expectations(self) -> asyncio.Future[tuple[int, bytes]]:
        loop = asyncio.get_running_loop()
        self._expected_resp_future = loop.create_future()
        return self._expected_resp_future

    def clear_handshake_expectations(self) -> None:
        if self._expected_resp_future and not self._expected_resp_future.done():
            self._expected_resp_future.cancel()
        self._expected_resp_future = None

    def notify_link_sync_Confirmation(self, cmd_id: int, payload: bytes) -> None:
        if self._expected_resp_future and not self._expected_resp_future.done():
            self._expected_resp_future.set_result((cmd_id, payload))

    def build_handshake_config_payload(self) -> bytes:
        msg = HandshakeConfig(
            ack_timeout_ms=self.ack_timeout_ms,
            ack_retry_limit=self.retry_limit,
            response_timeout_ms=self.response_timeout_ms,
        )
        return msg.SerializeToString()

    def compute_handshake_tag(self, nonce: bytes) -> bytes:
        return hmac.new(
            self._config.serial_shared_secret,
            nonce,
            "sha256",
        ).digest()[: protocol.HANDSHAKE_TAG_LENGTH]

    async def trigger_mcu_reset(self) -> float:
        script = "/usr/bin/reset-mcu"
        start = self.clock()
        if self.reset_hook is not None:
            return await self.reset_hook(start)

        if not os.path.exists(script):
            self._logger.debug("MCU reset script not present; skipping physical reset", path=script)
            return 0.0

        try:
            proc = await asyncio.create_subprocess_exec(
                script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode != 0:
                self._logger.warning(
                    "MCU reset script returned non-zero",
                    returncode=proc.returncode,
                    stderr=stderr.decode("utf-8", errors="replace").strip(),
                )
        except (OSError, asyncio.TimeoutError) as exc:
            self._logger.warning("Failed to execute MCU reset script", error=str(exc))

        await asyncio.sleep(self.post_reset_delay)
        return self.clock() - start

    async def synchronize_serial_link(self) -> bool:
        """Execute full three-way HMAC link synchronization sequence."""
        if self._handshake_lock.locked():
            self._logger.debug("Handshake already in progress; skipping duplicate request")
            return False

        async with self._handshake_lock:
            try:
                await self. trigger("begin_reset")  # type: ignore[attr-defined]
                await self.trigger_mcu_reset()
                await self.trigger("start_sync")  # type: ignore[attr-defined]

                nonce = secrets.token_bytes(protocol.HANDSHAKE_NONCE_LENGTH)
                self._active_nonce = nonce
                self._state.link_nonce_length = len(nonce)
                tag = self.compute_handshake_tag(nonce)
                config_bytes = self.build_handshake_config_payload()
                sync_payload = nonce + tag + config_bytes

                resp_future = self.prepare_handshake_expectations()
                await self.trigger("wait_response")  # type: ignore[attr-defined]

                sent = await self.send_frame(
                    Command.CMD_LINK_SYNC.value,
                    sync_payload,
                )
                if not sent:
                    await self._register_handshake_error("send_failed")
                    return False

                try:
                    timeout_sec = max(2.0, (self.response_timeout_ms / 1000.0) * 2.0)
                    cmd_id, resp_payload = await asyncio.wait_for(
                        resp_future,
                        timeout=timeout_sec,
                    )
                except asyncio.TimeoutError:
                    await self._register_handshake_error("response_timeout")
                    return False
                finally:
                    self.clear_handshake_expectations()

                if cmd_id == Command.CMD_LINK_SYNC_RESP.value:
                    return await self.handle_link_sync_resp(
                        0,
                        resp_payload,
                        from_sync_wait=True,
                    )

                await self._register_handshake_error(f"unexpected_cmd_{cmd_id}")
                return False

            except (RuntimeError, ValueError, OSError) as exc:
                self._logger.exception("Unhandled exception during serial link synchronization", error=str(exc))
                await self._register_handshake_error("internal_exception")
                return False

    async def handle_link_sync_resp(
        self,
        seq_id: int,
        payload: bytes,
        *,
        from_sync_wait: bool = False,
    ) -> bool:
        """Validate MCU's CMD_LINK_SYNC_RESP frame and finalize synchronization."""
        if not from_sync_wait and self._expected_resp_future and not self._expected_resp_future.done():
            self.notify_link_sync_Confirmation(Command.CMD_LINK_SYNC_RESP.value, payload)
            return True

        expected_nonce = self._active_nonce
        if not expected_nonce:
            self._logger.warning("Received unsolicited CMD_LINK_SYNC_RESP without active nonce")
            await self.acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                seq_id,
                status=Status.STATUS_ERROR,
            )  # type: ignore[call-arg]
            await self._register_handshake_error("unsolicited_resp")
            return False

        try:
            packet = LinkSyncPacket.from_bytes(payload)
        except ValueError as exc:
            self._logger.error(
                "[SIL-2] Malformed CMD_LINK_SYNC_RESP payload",
                error=str(exc),
                length=len(payload),
            )
            await self.acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                seq_id,
                status=Status.STATUS_MALFORMED,
            )  # type: ignore[call-arg]
            await self._register_handshake_error("malformed_resp", fatal=True)
            return False

        if not hmac.compare_digest(packet.nonce, expected_nonce):
            self._logger.error("[SIL-2] Handshake nonce mismatch detected")
            await self.acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                seq_id,
                status=Status.STATUS_NONCE_MISMATCH,
            )  # type: ignore[call-arg]
            await self._register_handshake_error("nonce_mismatch", fatal=True)
            return False

        expected_tag = self.compute_handshake_tag(expected_nonce)
        if not hmac.compare_digest(packet.tag, expected_tag):
            self._logger.error("[SIL-2] Handshake authentication tag mismatch")
            await self.acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                seq_id,
                status=Status.STATUS_AUTH_FAIL,
            )  # type: ignore[call-arg]
            await self._register_handshake_error("auth_mismatch", fatal=True)
            return False

        self._active_nonce = None
        self.clear_handshake_expectations()
        await self._handle_handshake_success()
        self._logger.info("MCU link synchronised", nonce=payload.hex())
        asyncio.create_task(self.fetch_capabilities_with_delay())
        return True

    async def fetch_capabilities_with_delay(self) -> None:
        if self.capabilities_delay > 0:
            await asyncio.sleep(self.capabilities_delay)
        await self.fetch_capabilities()

    async def fetch_capabilities(self) -> bool:
        cmd_id = Command.CMD_GET_CAPABILITIES.value
        self._logger.debug("Starting capabilities discovery", command_id=f"0x{cmd_id:02X}")

        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_exponential(
                multiplier=self.retry_backoff_base,
                max=self.retry_backoff_max,
            ),
            retry=tenacity.retry_if_exception_type(TimeoutError),
            before_sleep=tenacity.before_sleep_log(self._logger, logging.DEBUG),
            reraise=False,
        )

        async def _attempt() -> bool:
            res = await self.send_frame(cmd_id, b"")
            if res is False:
                raise TimeoutError("Send failed")
            try:
                self._parse_capabilities(res)
                return True
            except (ProtobufDecodeError, ValueError, TypeError, KeyError) as exc:
                self._logger.error("[SIL-2] Capabilities payload corrupt; aborting", error=str(exc))
                return False

        try:
            return await retryer(_attempt)
        except tenacity.RetryError as exc:
            self._logger.error("Capabilities exchange retries exhausted", error=str(exc))
            return False

    def _parse_capabilities(self, payload: bool | bytes | ProtobufMessage) -> None:
        raw = b"" if isinstance(payload, bool) else cast(bytes, payload)
        self._state.mcu_capabilities = (
            payload if isinstance(payload, pb.Capabilities) else pb.Capabilities.FromString(raw)
        )
        self._logger.info("MCU capabilities received", capabilities=str(self._state.mcu_capabilities))

    async def handle_handshake_failure(
        self,
        reason: str,
        *,
        detail: str | None = None,
        fatal: bool = False,
    ) -> None:
        await self._register_handshake_error(reason, detail=detail, fatal=fatal)

    async def _handle_handshake_success(self) -> None:
        await self.trigger("complete_sync")  # type: ignore[attr-defined]
        await self._publish_handshake_Diagnostic("synchronized")

    async def _register_handshake_error(
        self,
        reason: str,
        *,
        detail: str | None = None,
        fatal: bool = False,
    ) -> None:
        await self.trigger("fail_sync", reason=reason)  # type: ignore[attr-defined]
        if fatal:
            self.stats.fatal_errors += 1
            self._consecutive_fatal_failures += 1
            if self._consecutive_fatal_failures >= HANDSHAKE_FATAL_FAILURE_THRESHOLD:
                self._logger.critical(
                    "[SIL-2] Handshake fatal failure threshold reached; entering backoff",
                    streak=self._consecutive_fatal_failures,
                    reason=reason,
                )
        await self._publish_handshake_Diagnostic("failed", reason=reason, detail=detail)

    async def _publish_handshake_Diagnostic(
        self,
        event_type: str,
        *,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        # Publish diagnostic snapshot via MQTT enqueue hook if available
        pass

    def parse_handshake_ack_payload(self, payload: bytes) -> HandshakeAckPacket:
        return HandshakeAckPacket.from_bytes(payload)

    def validate_nonce_echo(self, echoed: bytes) -> bool:
        if not self._active_nonce:
            return False
        return hmac.compare_digest(echoed, self._active_nonce)

    def parse_timing_response(self, payload: bytes) -> tuple[int, int, int]:
        if len(payload) < 6:
            raise ValueError("Timing payload too short")
        ack_ms, retry_lim, resp_ms = struct.unpack(">HHH", payload[:6])
        return ack_ms, retry_lim, resp_ms
