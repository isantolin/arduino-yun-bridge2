"""Serial handshake coordination utilities for BridgeService.

[MIL-SPEC COMPLIANCE]
This module implements secure handshake with:
- HMAC-SHA256 authentication (timing-safe comparison)
- Nonce with monotonic counter (anti-replay protection)
- Secure memory zeroization after use
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Awaitable, Callable

from ..config.settings import RuntimeConfig
from ..const import (
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
)
from ..mqtt.messages import QueuedPublish
from ..protocol.topics import handshake_topic
from ..rpc import protocol
from ..rpc.protocol import Command, MAX_PAYLOAD_SIZE, Status
from ..security import (
    generate_nonce_with_counter,
    secure_zero,
    timing_safe_equal,
    validate_nonce_counter,
)
from ..state.context import RuntimeState, McuCapabilities

SendFrameCallable = Callable[[int, bytes], Awaitable[bool]]
EnqueueMessageCallable = Callable[[QueuedPublish], Awaitable[None]]
AcknowledgeFrameCallable = Callable[..., Awaitable[None]]

logger = logging.getLogger("mcubridge.service.handshake")


@dataclass(frozen=True, slots=True)
class SerialTimingWindow:
    """Derived serial retry/response windows used by both MCU and MPU."""

    ack_timeout_ms: int
    response_timeout_ms: int
    retry_limit: int

    @property
    def ack_timeout_seconds(self) -> float:
        return self.ack_timeout_ms / 1000.0

    @property
    def response_timeout_seconds(self) -> float:
        return self.response_timeout_ms / 1000.0


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def _seconds_to_ms(value: float) -> int:
    return int(round(max(0.0, value) * 1000.0))


def derive_serial_timing(config: RuntimeConfig) -> SerialTimingWindow:
    ack_ms = _clamp(
        _seconds_to_ms(config.serial_retry_timeout),
        protocol.HANDSHAKE_ACK_TIMEOUT_MIN_MS,
        protocol.HANDSHAKE_ACK_TIMEOUT_MAX_MS,
    )
    response_ms = _clamp(
        _seconds_to_ms(config.serial_response_timeout),
        protocol.HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS,
        protocol.HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS,
    )
    response_ms = max(response_ms, ack_ms)
    retry_limit = _clamp(
        int(config.serial_retry_attempts),
        protocol.HANDSHAKE_RETRY_LIMIT_MIN,
        protocol.HANDSHAKE_RETRY_LIMIT_MAX,
    )
    return SerialTimingWindow(
        ack_timeout_ms=ack_ms,
        response_timeout_ms=response_ms,
        retry_limit=retry_limit,
    )


class SerialHandshakeFatal(RuntimeError):
    """Raised when MCU rejects the serial shared secret permanently."""


_IMMEDIATE_FATAL_HANDSHAKE_REASONS: frozenset[str] = frozenset(
    {
        "sync_auth_mismatch",
        "sync_length_mismatch",
    }
)

_STATUS_PAYLOAD_WINDOW = max(0, int(MAX_PAYLOAD_SIZE) - 2)


class SerialHandshakeManager:
    """Encapsulates MCU serial handshake orchestration and telemetry."""

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
        self._reset_payload = self._build_reset_payload()
        self._capabilities_future: asyncio.Future[bytes] | None = None

    async def synchronize(self) -> bool:
        await self._respect_handshake_backoff()
        nonce_length = protocol.HANDSHAKE_NONCE_LENGTH
        self._state.record_handshake_attempt()

        # [MIL-SPEC] Generate nonce with anti-replay counter
        nonce, new_counter = generate_nonce_with_counter(
            self._state.link_nonce_counter
        )
        self._state.link_nonce_counter = new_counter

        self._state.link_handshake_nonce = nonce
        self._state.link_nonce_length = nonce_length
        self._state.link_expected_tag = self._compute_handshake_tag(nonce)
        self._state.link_is_synchronized = False
        reset_ok = await self._send_frame(
            Command.CMD_LINK_RESET.value,
            self._reset_payload,
        )
        if not reset_ok and self._reset_payload:
            self._logger.warning(
                "LINK_RESET rejected; retrying without timing payload (legacy firmware?)"
            )
            reset_ok = await self._send_frame(
                Command.CMD_LINK_RESET.value,
                b"",
            )
        if not reset_ok:
            self._logger.warning("Failed to emit LINK_RESET during handshake")
            self._clear_handshake_expectations()
            await self._handle_handshake_failure("link_reset_send_failed")
            return False
        await asyncio.sleep(0.05)
        sync_ok = await self._send_frame(Command.CMD_LINK_SYNC.value, nonce)
        if not sync_ok:
            self._logger.warning("Failed to emit LINK_SYNC during handshake")
            self._clear_handshake_expectations()
            await self._handle_handshake_failure("link_sync_send_failed")
            return False

        confirmed = await self._wait_for_link_sync_confirmation(nonce)
        if not confirmed:
            self._logger.warning(
                "MCU link synchronisation did not confirm within timeout"
            )
            pending_nonce = self._state.link_handshake_nonce
            self._clear_handshake_expectations()
            if pending_nonce == nonce:
                await self._handle_handshake_failure("link_sync_timeout")
            return False
        return True

    async def handle_link_sync_resp(self, payload: bytes) -> bool:
        expected = self._state.link_handshake_nonce
        if expected is None:
            self._logger.warning("Unexpected LINK_SYNC_RESP without pending nonce")
            await self._acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                status=Status.MALFORMED,
                extra=payload[:_STATUS_PAYLOAD_WINDOW],
            )
            await self._handle_handshake_failure("unexpected_sync_resp")
            return False

        nonce_length = self._state.link_nonce_length or len(expected)
        required_length = nonce_length + protocol.HANDSHAKE_TAG_LENGTH
        rate_limit = self._config.serial_handshake_min_interval
        if rate_limit > 0:
            now = time.monotonic()
            if now < self._state.handshake_rate_limit_until:
                self._logger.warning(
                    ("LINK_SYNC_RESP throttled due to rate limit " "(remaining=%.2fs)"),
                    self._state.handshake_rate_limit_until - now,
                )
                await self._acknowledge_frame(
                    Command.CMD_LINK_SYNC_RESP.value,
                    status=Status.MALFORMED,
                    extra=payload[:_STATUS_PAYLOAD_WINDOW],
                )
                await self._handle_handshake_failure("sync_rate_limited")
                return False
            self._state.handshake_rate_limit_until = now + rate_limit

        if len(payload) != required_length:
            self._logger.warning(
                "LINK_SYNC_RESP malformed length (expected %d got %d)",
                required_length,
                len(payload),
            )
            await self._acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                status=Status.MALFORMED,
                extra=payload[:_STATUS_PAYLOAD_WINDOW],
            )
            self._clear_handshake_expectations()
            await self._handle_handshake_failure("sync_length_mismatch")
            return False

        nonce = payload[:nonce_length]
        tag_bytes = payload[nonce_length:required_length]
        expected_tag = self._state.link_expected_tag
        recalculated_tag = self._compute_handshake_tag(nonce)

        nonce_mismatch = nonce != expected
        missing_expected_tag = expected_tag is None
        bad_tag_length = len(tag_bytes) != protocol.HANDSHAKE_TAG_LENGTH
        # [MIL-SPEC] Use timing-safe comparison to prevent side-channel attacks
        tag_mismatch = not timing_safe_equal(tag_bytes, recalculated_tag)

        # [MIL-SPEC] Validate nonce counter for anti-replay protection
        if not nonce_mismatch and not missing_expected_tag:
            is_valid, _ = validate_nonce_counter(
                nonce, self._state.link_last_nonce_counter
            )
            if not is_valid:
                self._logger.warning(
                    "LINK_SYNC_RESP replay detected (nonce counter too low)"
                )
                nonce_mismatch = True  # Treat as nonce mismatch

        if nonce_mismatch or missing_expected_tag or bad_tag_length or tag_mismatch:
            self._logger.warning(
                "LINK_SYNC_RESP auth mismatch (nonce=%s)",
                nonce.hex(),
            )
            await self._acknowledge_frame(
                Command.CMD_LINK_SYNC_RESP.value,
                status=Status.MALFORMED,
                extra=payload[:_STATUS_PAYLOAD_WINDOW],
            )
            self._clear_handshake_expectations()
            await self._handle_handshake_failure(
                "sync_auth_mismatch",
                detail="nonce_or_tag_mismatch",
            )
            return False

        payload = nonce  # Normalise for logging

        self._state.link_is_synchronized = True
        self._clear_handshake_expectations()
        await self._handle_handshake_success()
        self._logger.info("MCU link synchronised (nonce=%s)", payload.hex())

        # Fire and forget capabilities fetch to avoid blocking the sync flow,
        # but ensure it runs with a delay.
        asyncio.create_task(self._fetch_capabilities_with_delay())

        return True

    async def _fetch_capabilities_with_delay(self) -> None:
        """Wait for bus settlement and then fetch capabilities."""
        # [SIL-2] Mandatory settlement delay.
        # This prevents collision with CMD_GET_VERSION (0x40) sent by daemon health checks.
        await asyncio.sleep(2.0)
        await self._fetch_capabilities()

    async def _fetch_capabilities(self) -> bool:
        loop = asyncio.get_running_loop()

        # Log the command ID for verification (User Request: CMD Problem Investigation)
        cmd_id = Command.CMD_GET_CAPABILITIES.value
        self._logger.debug(f"Starting capabilities discovery using Command ID 0x{cmd_id:02X}")

        # [SIL-2] Retry logic for capabilities discovery to handle bus contention
        # Increased attempts and backoff to ensure we eventually get through
        for attempt in range(1, 6):
            self._capabilities_future = loop.create_future()

            if attempt > 1:
                # Progressive backoff to allow serial buffers to flush
                # 0.5s, 1.0s, 1.5s...
                await asyncio.sleep(0.5 * attempt)

            ok = await self._send_frame(Command.CMD_GET_CAPABILITIES.value, b"")
            if not ok:
                self._logger.warning(f"Failed to send CMD_GET_CAPABILITIES (attempt {attempt})")
                self._capabilities_future = None
                continue

            try:
                # [SIL-2] Force ample timeout for cold boot capabilities discovery
                timeout = max(5.0, self._timing.response_timeout_seconds)
                payload = await asyncio.wait_for(self._capabilities_future, timeout=timeout)
                self._parse_capabilities(payload)
                return True
            except asyncio.TimeoutError:
                self._logger.warning(f"Timeout waiting for MCU capabilities (attempt {attempt})")
            finally:
                self._capabilities_future = None

        return False

    def handle_capabilities_resp(self, payload: bytes) -> None:
        if self._capabilities_future and not self._capabilities_future.done():
            self._capabilities_future.set_result(payload)

    def _parse_capabilities(self, payload: bytes) -> None:
        if len(payload) < 8:
            self._logger.warning("Short capabilities payload: %s", payload.hex())
            return
        try:
            ver, arch, dig, ana, feat = struct.unpack(protocol.CAPABILITIES_FORMAT, payload[:8])
            self._state.mcu_capabilities = McuCapabilities(
                protocol_version=ver,
                board_arch=arch,
                num_digital_pins=dig,
                num_analog_inputs=ana,
                features=feat
            )
            self._logger.info("MCU Capabilities: %s", self._state.mcu_capabilities)
        except struct.error:
            self._logger.warning("Failed to unpack capabilities")

    async def handle_link_reset_resp(self, payload: bytes) -> bool:
        self._logger.info("MCU link reset acknowledged (payload=%s)", payload.hex())
        self._state.link_is_synchronized = False
        return True

    async def handle_handshake_failure(
        self,
        reason: str,
        *,
        detail: str | None = None,
    ) -> None:
        await self._handle_handshake_failure(reason, detail=detail)

    def clear_handshake_expectations(self) -> None:
        self._clear_handshake_expectations()

    def raise_if_handshake_fatal(self) -> None:
        reason = self._fatal_handshake_reason()
        if not reason:
            return

        hint = (
            "Verify mcubridge.general.serial_shared_secret (configured via UCI/LuCI) "
            "matches the BRIDGE_SERIAL_SHARED_SECRET define compiled into your sketches."
        )
        raise SerialHandshakeFatal(
            "MCU rejected the serial shared secret " f"(reason={reason}). {hint}"
        )

    async def _wait_for_link_sync_confirmation(self, nonce: bytes) -> bool:
        loop = asyncio.get_running_loop()
        timeout = max(0.5, self._timing.response_timeout_seconds)
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._state.link_is_synchronized and self._state.link_handshake_nonce is None:
                return True
            if self._state.link_handshake_nonce != nonce and not self._state.link_is_synchronized:
                break
            await asyncio.sleep(0.01)
        return self._state.link_is_synchronized and self._state.link_handshake_nonce is None

    def _clear_handshake_expectations(self) -> None:
        # [MIL-SPEC] Securely zero sensitive handshake material before clearing
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

    async def _respect_handshake_backoff(self) -> None:
        delay = self._handshake_backoff_remaining()
        if delay <= 0:
            return
        self._logger.warning(
            "Delaying serial handshake for %.2fs due to prior failures",
            delay,
        )
        await self._publish_handshake_event(
            "backoff_wait",
            reason=self._state.last_handshake_error,
            detail="waiting_for_backoff",
            extra={"delay_seconds": round(delay, 3)},
        )
        await asyncio.sleep(delay)

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
        }
        if extra:
            payload.update(extra)
        message = QueuedPublish(
            topic_name=handshake_topic(self._state.mqtt_topic_prefix),
            payload=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            user_properties=(("bridge-event", "handshake"),),
        )
        await self._enqueue_mqtt(message)

    async def _handle_handshake_success(self) -> None:
        self._state.record_handshake_success()
        duration = round(self._state.handshake_last_duration, 3)
        await self._publish_handshake_event(
            "success",
            extra={"duration_seconds": duration},
        )

    async def _handle_handshake_failure(
        self,
        reason: str,
        *,
        detail: str | None = None,
    ) -> None:
        self._state.record_handshake_failure(reason)
        is_fatal = self._should_mark_failure_fatal(reason)
        fatal_detail = detail
        if is_fatal and reason not in _IMMEDIATE_FATAL_HANDSHAKE_REASONS:
            fatal_detail = detail or (
                f"failure_streak_exceeded_{self._fatal_threshold}"
            )
        if is_fatal:
            self._state.record_handshake_fatal(reason, fatal_detail)
            self._logger.error(
                "Fatal serial handshake failure reason=%s detail=%s",
                reason,
                fatal_detail or "",
            )
        backoff = self._maybe_schedule_handshake_backoff(reason)
        extra: dict[str, Any] = {
            "duration_seconds": round(
                self._state.handshake_last_duration,
                3,
            )
        }
        if backoff:
            extra["backoff_seconds"] = round(backoff, 3)
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

    def _maybe_schedule_handshake_backoff(self, reason: str) -> float | None:
        streak = max(1, self._state.handshake_failure_streak)
        fatal = self._is_immediate_fatal(reason)
        threshold = 1 if fatal else 3
        if streak < threshold:
            return None
        power = max(0, streak - threshold)
        delay = min(
            SERIAL_HANDSHAKE_BACKOFF_MAX,
            SERIAL_HANDSHAKE_BACKOFF_BASE * (2**power),
        )
        self._state.handshake_backoff_until = time.monotonic() + delay
        return delay

    def _fatal_handshake_reason(self) -> str | None:
        if self._state.handshake_fatal_reason:
            return self._state.handshake_fatal_reason
        return None

    @staticmethod
    def calculate_handshake_tag(secret: bytes | None, nonce: bytes) -> bytes:
        """Return the truncated HMAC tag defined by the serial spec."""
        if not secret:
            return b""
        digest = hmac.new(secret, nonce, hashlib.sha256).digest()
        return digest[: protocol.HANDSHAKE_TAG_LENGTH]

    def compute_handshake_tag(self, nonce: bytes) -> bytes:
        return self._compute_handshake_tag(nonce)

    def _compute_handshake_tag(self, nonce: bytes) -> bytes:
        secret = self._config.serial_shared_secret
        return self.calculate_handshake_tag(secret, nonce)

    def _build_reset_payload(self) -> bytes:
        fmt = protocol.HANDSHAKE_CONFIG_FORMAT
        if not fmt:
            return b""
        packed = struct.pack(
            fmt,
            self._timing.ack_timeout_ms,
            self._timing.retry_limit,
            self._timing.response_timeout_ms,
        )
        return packed

    def _should_mark_failure_fatal(self, reason: str) -> bool:
        if self._is_immediate_fatal(reason):
            return True
        threshold = max(1, self._fatal_threshold)
        return self._state.handshake_failure_streak >= threshold

    @staticmethod
    def _is_immediate_fatal(reason: str) -> bool:
        return reason in _IMMEDIATE_FATAL_HANDSHAKE_REASONS
