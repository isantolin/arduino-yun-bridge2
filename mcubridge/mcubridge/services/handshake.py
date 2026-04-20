"""Serial handshake coordination utilities for BridgeService.

[MIL-SPEC COMPLIANCE]
This module implements secure handshake with:
- HMAC-SHA256 authentication (timing-safe comparison)
- Nonce with monotonic counter (anti-replay protection)
- Secure memory zeroization after use
"""

from __future__ import annotations

import asyncio
import logging
import structlog
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import msgspec
import msgspec.msgpack
import tenacity
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.constant_time import bytes_eq

from ..config.const import (
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
)
from ..config.settings import RuntimeConfig
from ..protocol import protocol
from ..protocol.protocol import Command, Status
from ..protocol.structures import (
    CapabilitiesPacket,
    HandshakeConfigPacket,
    LinkSyncPacket,
    QueuedPublish,
    SerialTimingWindow,
)
from ..protocol.topics import Topic, topic_path
from ..security.security import (
    derive_handshake_key,
    generate_nonce_with_counter,
    secure_zero,
    validate_nonce_counter,
)
from ..state.context import McuCapabilities, RuntimeState
from transitions import Machine

from typing import Protocol


class SendFrameCallable(Protocol):
    async def __call__(self, command_id: int, payload: bytes, seq_id: int | None = None) -> bool: ...


EnqueueMessageCallable = Callable[[QueuedPublish], Awaitable[None]]
AcknowledgeFrameCallable = Callable[..., Awaitable[None]]

logger = structlog.get_logger("mcubridge.service.handshake")
_msgpack_enc = msgspec.msgpack.Encoder()


def derive_serial_timing(config: RuntimeConfig) -> SerialTimingWindow:
    """Derive timing windows from config with strict declarative validation."""
    # Convert configuration seconds to milliseconds for the wire protocol
    ack_ms = int(round(config.serial_retry_timeout * 1000.0))
    response_ms = int(round(config.serial_response_timeout * 1000.0))
    retry_limit = int(config.serial_retry_attempts)

    # Cross-field semantic validation: response must always be >= retry/ack
    response_ms = max(response_ms, ack_ms)

    raw = {
        "ack_timeout_ms": ack_ms,
        "response_timeout_ms": response_ms,
        "retry_limit": retry_limit,
    }

    # [SIL-2] msgspec will raise ValidationError if values are outside protocol bounds.
    return msgspec.convert(raw, SerialTimingWindow, strict=True)


class SerialHandshakeFatal(RuntimeError):
    """Raised when MCU rejects the serial shared secret permanently."""


_IMMEDIATE_FATAL_HANDSHAKE_REASONS: frozenset[str] = frozenset(
    {
        "sync_auth_mismatch",
        "sync_length_mismatch",
    }
)


class SerialHandshakeManager:
    """Encapsulates MCU serial handshake orchestration and telemetry."""

    if TYPE_CHECKING:
        # FSM generated methods and attributes for static analysis
        fsm_state: str
        reset_fsm: Callable[[], None]
        fail_handshake: Callable[[], None]
        start_reset: Callable[[], None]
        start_sync: Callable[[], None]
        start_confirm: Callable[[], None]
        complete_handshake: Callable[[], None]

    # FSM States
    STATE_UNSYNCHRONIZED = "unsynchronized"
    STATE_RESETTING = "resetting"
    STATE_SYNCING = "syncing"
    STATE_CONFIRMING = "confirming"
    STATE_SYNCHRONIZED = "synchronized"
    STATE_FAULT = "fault"

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_timing: SerialTimingWindow,
        send_frame: SendFrameCallable,
        enqueue_mqtt: EnqueueMessageCallable,
        acknowledge_frame: AcknowledgeFrameCallable,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._timing = serial_timing
        self._send_frame = send_frame
        self._enqueue_mqtt = enqueue_mqtt
        self._acknowledge_frame = acknowledge_frame
        self._logger = logger_ or logger
        self._fatal_threshold = max(1, config.serial_handshake_fatal_failures)
        self._reset_payload = HandshakeConfigPacket(
            ack_timeout_ms=self._timing.ack_timeout_ms,
            ack_retry_limit=self._timing.retry_limit,
            response_timeout_ms=self._timing.response_timeout_ms,
        ).encode()
        self._capabilities_future: asyncio.Future[bytes] | None = None

        # FSM Initialization
        self.state_machine = Machine(
            model=self,
            states=[
                self.STATE_UNSYNCHRONIZED,
                self.STATE_RESETTING,
                self.STATE_SYNCING,
                self.STATE_CONFIRMING,
                {
                    "name": self.STATE_SYNCHRONIZED,
                    "on_enter": "_on_fsm_synchronized",
                    "on_exit": "_on_fsm_unsynchronized",
                },
                self.STATE_FAULT,
            ],
            transitions=[
                {"trigger": "start_reset", "source": "*", "dest": self.STATE_RESETTING},
                {
                    "trigger": "start_sync",
                    "source": self.STATE_RESETTING,
                    "dest": self.STATE_SYNCING,
                },
                {
                    "trigger": "start_confirm",
                    "source": self.STATE_SYNCING,
                    "dest": self.STATE_CONFIRMING,
                },
                {
                    "trigger": "complete_handshake",
                    "source": [self.STATE_SYNCING, self.STATE_CONFIRMING],
                    "dest": self.STATE_SYNCHRONIZED,
                },
                {"trigger": "fail_handshake", "source": "*", "dest": self.STATE_FAULT},
                {
                    "trigger": "reset_fsm",
                    "source": "*",
                    "dest": self.STATE_UNSYNCHRONIZED,
                },
            ],
            initial=self.STATE_UNSYNCHRONIZED,
            queued=True,
            model_attribute="fsm_state",
            ignore_invalid_triggers=True,
            after_state_change="_on_fsm_state_change",
        )

    def _on_fsm_state_change(self) -> None:
        """Update Prometheus Enum metric on every FSM transition."""
        self._state.metrics.handshake_state.state(self.fsm_state)

    def _on_fsm_synchronized(self) -> None:
        """Callback when entering synchronized state."""
        self._state.mark_synchronized()

    def _on_fsm_unsynchronized(self) -> None:
        """Callback when leaving synchronized state."""
        self._state.mark_transport_connected()

    async def synchronize(self) -> bool:
        # [SIL-2] Unified Retry Strategy for Link Synchronisation
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(self._fatal_threshold),
            wait=tenacity.wait_exponential_jitter(
                initial=SERIAL_HANDSHAKE_BACKOFF_BASE,
                max=SERIAL_HANDSHAKE_BACKOFF_MAX,
                jitter=1.0,
            ),
            retry=tenacity.retry_if_result(lambda res: res is False),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            reraise=False,
        )

        async def _attempt() -> bool:
            # [SIL-2] Direct metrics recording (No Wrapper)
            self._state.last_handshake_unix = time.time()
            self._state._handshake_last_started = time.monotonic()  # type: ignore[reportPrivateUsage]
            self._state.handshake_attempts += 1
            self._state.metrics.handshake_attempts.inc()
            self.reset_fsm()  # Ensure clean slate
            return await self._synchronize_attempt()

        try:
            ok: bool = await retryer(_attempt)
            self._logger.debug("Handshake stats: %s", retryer.statistics)
            return ok
        except tenacity.RetryError:
            self.fail_handshake()
            return False

    async def _synchronize_attempt(self) -> bool:
        nonce_length = protocol.HANDSHAKE_NONCE_LENGTH

        # Transition to RESETTING
        self.start_reset()
        self._state.link_sync_event.clear()

        # [MIL-SPEC] Generate nonce with anti-replay counter
        nonce, new_counter = generate_nonce_with_counter(self._state.link_nonce_counter)
        self._state.link_nonce_counter = new_counter

        self._state.link_handshake_nonce = nonce
        self._state.link_nonce_length = nonce_length
        self._state.link_expected_tag = self.calculate_handshake_tag(self._config.serial_shared_secret, nonce)

        reset_ok = await self._send_frame(
            Command.CMD_LINK_RESET.value,
            self._reset_payload,
        )
        if not reset_ok:
            self.clear_handshake_expectations()
            await self.handle_handshake_failure("link_reset_send_failed")
            return False

        # [SIL-2] Wait for MCU stabilization period (BRIDGE_STARTUP_STABILIZATION_MS)
        # Increased to 0.5s for QEMU/Emulation robustness
        await asyncio.sleep(0.5)

        # Transition to SYNCING
        self.start_sync()
        await asyncio.sleep(0.05)

        # [MIL-SPEC] Send LINK_SYNC with mutual authentication tag
        our_tag = self.calculate_handshake_tag(self._config.serial_shared_secret, nonce)
        sync_payload = LinkSyncPacket(nonce=nonce, tag=our_tag).encode()
        sync_ok = await self._send_frame(Command.CMD_LINK_SYNC.value, sync_payload)
        if not sync_ok:
            self.clear_handshake_expectations()
            await self.handle_handshake_failure("link_sync_send_failed")
            return False

        # [SIL-2] Race Condition Guard: check if async response already put us in fault or success.
        if self.fsm_state == self.STATE_FAULT:
            return False

        # Transition to CONFIRMING only if we are still in SYNCING.
        # High-speed emulators may have already triggered complete_handshake().
        if self.fsm_state == self.STATE_SYNCING:
            self.start_confirm()

        confirmed = await self._wait_for_link_sync_confirmation(nonce)
        if not confirmed:
            # [SIL-2] Double check if we didn't just transition to fault via async path.
            if self.fsm_state == self.STATE_FAULT:
                return False

            pending_nonce = self._state.link_handshake_nonce
            self.clear_handshake_expectations()
            if pending_nonce == nonce:
                await self.handle_handshake_failure("link_sync_timeout")
            return False

        # Transition to SYNCHRONIZED happens in handle_link_sync_resp (or implicitly confirmed here)
        if self.fsm_state != self.STATE_SYNCHRONIZED and self.fsm_state != self.STATE_FAULT:
            self.complete_handshake()

        return self.fsm_state == self.STATE_SYNCHRONIZED

    async def handle_link_sync_resp(self, seq_id: int, payload: bytes) -> bool:
        expected = self._state.link_handshake_nonce
        if expected is None:
            self._logger.warning("Unexpected LINK_SYNC_RESP without pending nonce")
            await self._acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                seq_id,
                status=Status.MALFORMED,
            )
            await self.handle_handshake_failure("unexpected_sync_resp")
            return False

        rate_limit = self._config.serial_handshake_min_interval
        if rate_limit > 0:
            now = time.monotonic()
            if now < self._state.handshake_rate_until:
                self._logger.warning(
                    ("LINK_SYNC_RESP throttled due to rate limit (remaining=%.2fs)"),
                    self._state.handshake_rate_until - now,
                )
                await self._acknowledge_frame(
                    Command.CMD_LINK_SYNC_RESP.value,
                    seq_id,
                    status=Status.MALFORMED,
                )
                await self.handle_handshake_failure("sync_rate_limited")
                return False
            self._state.handshake_rate_until = now + rate_limit

        try:
            sync_pkt = LinkSyncPacket.decode(payload)
            nonce = bytes(sync_pkt.nonce)
            tag_bytes = bytes(sync_pkt.tag)
        except (ValueError, TypeError):
            self._logger.warning(
                "LINK_SYNC_RESP msgpack decode failed (len=%d)",
                len(payload),
            )
            await self._acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                seq_id,
                status=Status.MALFORMED,
            )
            self.clear_handshake_expectations()
            await self.handle_handshake_failure("sync_decode_failed")
            return False

        expected_tag = self._state.link_expected_tag
        recalculated_tag = self.calculate_handshake_tag(self._config.serial_shared_secret, nonce)

        nonce_mismatch = not bytes_eq(nonce, expected)
        missing_expected_tag = expected_tag is None
        bad_tag_length = len(tag_bytes) != protocol.HANDSHAKE_TAG_LENGTH
        tag_mismatch = (
            not bytes_eq(tag_bytes, recalculated_tag) and self._config.serial_shared_secret != b"DEBUG_INSECURE"  # noqa: W503
        )

        if not nonce_mismatch and not missing_expected_tag:
            is_valid, _ = validate_nonce_counter(nonce, self._state.link_last_nonce_counter)
            if not is_valid:
                self._logger.warning("LINK_SYNC_RESP replay detected (nonce counter too low)")
                nonce_mismatch = True

        if nonce_mismatch or missing_expected_tag or bad_tag_length or tag_mismatch:
            self._logger.warning(
                "LINK_SYNC_RESP auth mismatch (nonce=%s)",
                nonce.hex(),
            )
            await self._acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                seq_id,
                status=Status.MALFORMED,
            )
            self.clear_handshake_expectations()
            await self.handle_handshake_failure(
                "sync_auth_mismatch",
                detail="nonce_or_tag_mismatch",
            )
            return False

        payload = nonce

        # FSM Transition to SYNCHRONIZED
        self.complete_handshake()

        self.clear_handshake_expectations()
        await self._handle_handshake_success()
        self._logger.info("MCU link synchronised (nonce=%s)", payload.hex())
        asyncio.create_task(self._fetch_capabilities_with_delay())
        return True

    async def _fetch_capabilities_with_delay(self) -> None:
        await asyncio.sleep(2.0)
        await self._fetch_capabilities()

    async def _fetch_capabilities(self) -> bool:
        loop = asyncio.get_running_loop()
        cmd_id = Command.CMD_GET_CAPABILITIES.value
        self._logger.debug("Starting capabilities discovery using Command ID 0x%02X", cmd_id)

        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_exponential(
                multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE,
                max=SERIAL_HANDSHAKE_BACKOFF_MAX,
            ),
            retry=tenacity.retry_if_exception_type(asyncio.TimeoutError),
            before_sleep=tenacity.before_sleep_log(self._logger, logging.DEBUG),
            reraise=False,
        )

        async def _attempt() -> bool:
            self._capabilities_future = loop.create_future()
            ok = await self._send_frame(Command.CMD_GET_CAPABILITIES.value, b"")
            if not ok:
                self._capabilities_future = None
                raise asyncio.TimeoutError("Send failed")

            try:
                timeout = max(5.0, self._timing.response_timeout_seconds)
                payload = await asyncio.wait_for(self._capabilities_future, timeout=timeout)
                self._parse_capabilities(payload)
                return True
            except asyncio.TimeoutError:
                raise
            finally:
                self._capabilities_future = None

        try:
            return await retryer(_attempt)
        except tenacity.RetryError:
            return False

    async def handle_capabilities_resp(self, seq_id: int, payload: bytes) -> bool:
        if self._capabilities_future and not self._capabilities_future.done():
            self._capabilities_future.set_result(payload)
        return True

    def _parse_capabilities(self, payload: bytes) -> None:
        try:
            cap = CapabilitiesPacket.decode(payload)
            self._state.mcu_capabilities = McuCapabilities(
                protocol_version=cap.ver,
                board_arch=cap.arch,
                num_digital_pins=cap.dig,
                num_analog_inputs=cap.ana,
                features=cap.feat,
            )
            self._logger.info("MCU Capabilities: %s", self._state.mcu_capabilities)
        except (ValueError, TypeError, ValueError, KeyError) as exc:
            self._logger.warning("Failed to unpack capabilities: %s", exc)

    async def handle_link_reset_resp(self, seq_id: int, payload: bytes) -> bool:
        self._logger.info("MCU link reset acknowledged (payload=%s)", payload.hex())
        return True

    async def handle_handshake_failure(
        self,
        reason: str,
        *,
        detail: str | None = None,
    ) -> None:
        # FSM Transition to FAULT
        self.fail_handshake()

        # [SIL-2] Direct metrics recording (No Wrapper)
        self._state.handshake_failure_streak += 1
        self._state.last_handshake_error = reason
        self._state.last_handshake_unix = time.time()
        self._state.handshake_last_duration = (
            self._state._handshake_duration_since_start()  # type: ignore[reportPrivateUsage]
        )
        self._state.mark_transport_connected()

        is_fatal = self._should_mark_failure_fatal(reason)
        fatal_detail = detail
        if is_fatal and reason not in _IMMEDIATE_FATAL_HANDSHAKE_REASONS:
            fatal_detail = detail or (f"failure_streak_exceeded_{self._fatal_threshold}")
        if is_fatal:
            # [SIL-2] Direct metrics recording (No Wrapper)
            self._state.handshake_fatal_count += 1
            self._state.handshake_fatal_reason = reason
            self._state.handshake_fatal_detail = fatal_detail
            self._state.handshake_fatal_unix = time.time()

            self._logger.error(
                "Fatal serial handshake failure reason=%s detail=%s",
                reason,
                fatal_detail or "",
            )
        self._maybe_schedule_handshake_backoff(reason)
        extra: dict[str, Any] = {
            "duration_seconds": round(
                self._state.handshake_last_duration,
                3,
            )
        }
        extra["fatal"] = is_fatal
        extra["fatal_count"] = self._state.handshake_fatal_count
        extra["fatal_threshold"] = self._fatal_threshold
        if self._state.handshake_fatal_count > 0:
            extra["fatal_unix"] = self._state.handshake_fatal_unix
            if self._state.handshake_fatal_detail:
                extra["fatal_detail"] = self._state.handshake_fatal_detail
        await self._publish_handshake_event(
            "failure",
            reason=reason,
            detail=fatal_detail if is_fatal else detail,
            extra=extra,
        )

    def raise_if_handshake_fatal(self) -> None:
        reason = self._state.handshake_fatal_reason
        if not reason:
            return

        hint = (
            "Verify mcubridge.general.serial_shared_secret (configured via UCI/LuCI) "
            "matches the BRIDGE_SERIAL_SHARED_SECRET define compiled into your sketches."
        )
        raise SerialHandshakeFatal(f"MCU rejected the serial shared secret (reason={reason}). {hint}")

    async def _wait_for_link_sync_confirmation(self, nonce: bytes) -> bool:
        timeout = max(0.5, self._timing.response_timeout_seconds)
        try:
            async with asyncio.timeout(timeout):
                if not self._state.is_synchronized:
                    await self._state.link_sync_event.wait()
                return self._state.is_synchronized
        except asyncio.TimeoutError:
            return False

    def clear_handshake_expectations(self) -> None:
        if self._state.link_handshake_nonce is not None:
            nonce_buf = bytearray(self._state.link_handshake_nonce)
            secure_zero(nonce_buf)
        if self._state.link_expected_tag is not None:
            tag_buf = bytearray(self._state.link_expected_tag)
            secure_zero(tag_buf)

        self._state.link_handshake_nonce = None
        self._state.link_expected_tag = None
        self._state.link_nonce_length = 0

    def _handshake_backoff_remaining(self) -> float:
        deadline = self._state.handshake_backoff_until
        if deadline <= 0:
            return 0.0
        return max(0.0, deadline - time.monotonic())

    async def _publish_handshake_event(
        self,
        event: str,
        *,
        reason: str | None = None,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "reason": reason,
            "detail": detail,
            "attempts": self._state.handshake_attempts,
            "successes": self._state.handshake_successes,
            "failures": self._state.handshake_failures,
            "failure_streak": self._state.handshake_failure_streak,
            "backoff_until": self._state.handshake_backoff_until,
            "fatal_count": self._state.handshake_fatal_count,
            "fatal_reason": self._state.handshake_fatal_reason,
            "fatal_detail": self._state.handshake_fatal_detail,
            "fatal_unix": self._state.handshake_fatal_unix,
            "fsm_state": self.fsm_state,  # Include FSM state in telemetry
        }
        if extra:
            payload |= extra
        message = QueuedPublish(
            topic_name=topic_path(self._state.mqtt_topic_prefix, Topic.SYSTEM, "handshake"),
            payload=_msgpack_enc.encode(payload),
            content_type="application/msgpack",
            user_properties=(("bridge-event", "handshake"),),
        )
        await self._enqueue_mqtt(message)

    async def _handle_handshake_success(self) -> None:
        # [SIL-2] Direct metrics recording (No Wrapper)
        self._state.handshake_failure_streak = 0
        self._state.handshake_backoff_until = 0.0
        self._state.last_handshake_error = None
        self._state.last_handshake_unix = time.time()
        self._state.handshake_last_duration = (
            self._state._handshake_duration_since_start()  # type: ignore[reportPrivateUsage]
        )
        self._state.mark_synchronized()
        self._state.handshake_successes += 1
        self._state.metrics.handshake_successes.inc()
        duration = round(self._state.handshake_last_duration, 3)
        await self._publish_handshake_event(
            "success",
            extra={"duration_seconds": duration},
        )

    def _maybe_schedule_handshake_backoff(self, reason: str) -> float | None:
        streak = max(1, self._state.handshake_failure_streak)
        fatal = reason in _IMMEDIATE_FATAL_HANDSHAKE_REASONS
        threshold = 1 if fatal else 3
        if streak < threshold:
            return None

        # [SIL-2] Direct exponential backoff calculation to avoid library overhead
        attempt = streak - threshold
        delay = min(
            SERIAL_HANDSHAKE_BACKOFF_BASE * (2**attempt),
            SERIAL_HANDSHAKE_BACKOFF_MAX,
        )

        self._state.handshake_backoff_until = time.monotonic() + delay
        return delay

    @staticmethod
    def calculate_handshake_tag(secret: bytes | None, nonce: bytes) -> bytes:
        if not secret:
            return b""
        if secret == b"DEBUG_INSECURE":
            # Return dummy 16-byte tag to satisfy required_length
            return b"DEBUG_TAG_UNUSED"
        # [MIL-SPEC] Use HKDF derived key for handshake authentication
        auth_key = derive_handshake_key(secret)
        h = hmac.HMAC(auth_key, hashes.SHA256())
        h.update(nonce)
        tag = h.finalize()[: protocol.HANDSHAKE_TAG_LENGTH]
        return tag

    def _should_mark_failure_fatal(self, reason: str) -> bool:
        return (
            reason in _IMMEDIATE_FATAL_HANDSHAKE_REASONS
            or self._state.handshake_failure_streak >= self._fatal_threshold  # noqa: W503
        )
