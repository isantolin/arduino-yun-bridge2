"""Handshake logic for the serial link."""

from __future__ import annotations

import asyncio
import hmac
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from yunbridge.config.settings import RuntimeConfig
from yunbridge.rpc import protocol
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.serial_flow import SerialTimingWindow
from yunbridge.state.context import RuntimeState

logger = logging.getLogger("yunbridge.handshake")

SERIAL_NONCE_LENGTH = 16


class SerialHandshakeFatal(Exception):
    """Raised when the handshake fails irrecoverably."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"Serial handshake fatal error: {reason} ({detail})")
        self.reason = reason
        self.detail = detail


def derive_serial_timing(config: RuntimeConfig) -> SerialTimingWindow:
    """Derive timing parameters for the serial link from config."""
    # Ensure values are within protocol bounds (u16/u32)
    ack_timeout = int(min(65535, max(10, config.serial_response_timeout * 1000)))
    retry_limit = int(min(255, max(1, config.serial_retry_attempts)))
    # For response timeout, we want a reasonable upper bound
    response_timeout = int(min(4294967295, max(100, config.process_timeout * 1000)))

    return SerialTimingWindow(
        ack_timeout_ms=ack_timeout,
        retry_limit=retry_limit,
        response_timeout_ms=response_timeout,
    )


async def handle_sync_response(
    config: RuntimeConfig,
    state: RuntimeState,
    payload: bytes,
) -> bool:
    """Process a LINK_SYNC_RESP frame from the MCU."""
    if not state.link_handshake_nonce:
        logger.warning("Received unexpected LINK_SYNC_RESP (no nonce sent)")
        return True

    expected_nonce_len = len(state.link_handshake_nonce)
    expected_tag_len = 16 if config.serial_shared_secret else 0
    expected_total = expected_nonce_len + expected_tag_len

    if len(payload) != expected_total:
        logger.warning(
            "LINK_SYNC_RESP malformed length (expected %d got %d)",
            expected_total,
            len(payload),
        )
        await _handle_handshake_failure(state, config, "sync_length_mismatch")
        return True

    received_nonce = payload[:expected_nonce_len]
    if received_nonce != state.link_handshake_nonce:
        logger.warning("LINK_SYNC_RESP nonce mismatch")
        await _handle_handshake_failure(state, config, "sync_nonce_mismatch")
        return True

    if expected_tag_len > 0:
        received_tag = payload[expected_nonce_len:]
        if not hmac.compare_digest(received_tag, state.link_expected_tag):
            logger.warning(
                "LINK_SYNC_RESP auth mismatch (nonce=%s)",
                state.link_handshake_nonce.hex(),
            )
            await _handle_handshake_failure(
                state, config, "sync_auth_mismatch", "nonce_or_tag_mismatch"
            )
            return True

    # Check rate limiting
    now = time.monotonic()
    if now < state.handshake_backoff_until:
        remaining = state.handshake_backoff_until - now
        logger.warning(
            "LINK_SYNC_RESP throttled due to rate limit (remaining=%.2fs)", remaining
        )
        await _handle_handshake_failure(state, config, "sync_rate_limited")
        return False  # Do not ACK to force retry later? Or ACK and ignore?
                      # Standard practice: ACK but don't transition state.
                      # But returning False here typically means "not handled",
                      # causing an error response. We probably want to return True
                      # but NOT set is_synchronized.
        return True

    logger.info(
        "MCU link synchronised (nonce=%s)", state.link_handshake_nonce.hex()
    )
    state.link_is_synchronized = True
    state.handshake_successes += 1
    state.handshake_failure_streak = 0
    state.link_handshake_nonce = None
    state.link_expected_tag = b""
    return True


async def _handle_handshake_failure(
    state: RuntimeState,
    config: RuntimeConfig,
    reason: str,
    detail: str = "",
) -> None:
    """Update state and potentially raise fatal error on repeated failures."""
    state.handshake_failures += 1
    state.handshake_failure_streak += 1
    state.last_handshake_error = reason

    # Exponential backoff
    delay = min(60.0, config.serial_handshake_min_interval * (2 ** (state.handshake_failure_streak - 1)))
    state.handshake_backoff_until = time.monotonic() + delay

    if state.handshake_failure_streak >= config.serial_handshake_fatal_failures:
        state.handshake_fatal_count += 1
        state.handshake_fatal_reason = reason
        state.handshake_fatal_detail = detail
        state.handshake_fatal_unix = time.time()
        detail_msg = f" ({detail})" if detail else ""
        error_msg = f"failure_streak_exceeded_{state.handshake_failure_streak}"
        if detail:
             error_msg = detail
        
        # Reset streak so we don't loop fatal errors instantly if caught
        # (though typically this raises exception up the stack)
        raise SerialHandshakeFatal(reason, error_msg)


async def attempt_handshake(
    config: RuntimeConfig,
    state: RuntimeState,
    send_frame: Callable[[int, bytes], Awaitable[bool]],
    compute_tag: Callable[[bytes], bytes],
) -> None:
    """
    Perform the link synchronization handshake sequence.
    
    1. Send CMD_LINK_RESET with configuration parameters.
    2. Wait (implicitly) for processing time.
    3. Generate a random nonce.
    4. Send CMD_LINK_SYNC with the nonce.
    5. The response is handled asynchronously by handle_sync_response via dispatcher.
    """
    state.link_is_synchronized = False
    
    # 1. Send Link Reset with Config
    timing = derive_serial_timing(config)
    
    # Payload format:
    # u16 ack_timeout_ms (big endian)
    # u8  retry_limit
    # u32 response_timeout_ms (big endian)
    config_payload = struct.pack(
        protocol.HANDSHAKE_CONFIG_FORMAT,
        timing.ack_timeout_ms,
        timing.retry_limit,
        timing.response_timeout_ms
    )
    
    logger.debug("Sending LINK_RESET...")
    if not await send_frame(Command.CMD_LINK_RESET.value, config_payload):
        logger.warning("Failed to send LINK_RESET")
        return

    # Small pause to let MCU apply config
    await asyncio.sleep(0.2)

    # 2. Prepare Sync
    import os
    nonce = os.urandom(SERIAL_NONCE_LENGTH)
    
    # Avoid zero-nonce if possible, though unlikely
    if all(b == 0 for b in nonce):
        nonce = b"\x01" * SERIAL_NONCE_LENGTH
        
    state.link_handshake_nonce = nonce
    state.link_nonce_length = len(nonce)
    state.link_expected_tag = compute_tag(nonce)
    
    logger.debug("Sending LINK_SYNC (nonce=%s)...", nonce.hex())
    if not await send_frame(Command.CMD_LINK_SYNC.value, nonce):
        logger.warning("Failed to send LINK_SYNC")
        return

    # Wait for sync confirmation
    # The actual state update happens in handle_sync_response which is called
    # by the main dispatcher loop when the response frame arrives.
    try:
        # We wait up to 3 seconds for the sync to become true
        for _ in range(30):
            if state.link_is_synchronized:
                return
            await asyncio.sleep(0.1)
            
        logger.warning("MCU link synchronisation did not confirm within timeout")
        await _handle_handshake_failure(state, config, "link_sync_timeout")
        
    except asyncio.CancelledError:
        raise
