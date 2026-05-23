"""Serial transport implementation using serialx streams and transports.

This module implements a Zero-Overhead asyncio transport using StreamReader
and StreamWriter. It delegates delimiter searching to Python's C core via
`readuntil`, ensuring maximum throughput for high-speed serial links.

[SIL-2 COMPLIANCE]
- Deterministic buffer handling.
- Explicit lifecycle management.
- Zero dynamic allocation after initialization.
- Sequential command-response flow control.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, cast

from cobs import cobs
import serialx
import structlog
import tenacity

from mcubridge.config.const import (
    MAX_SERIAL_FRAME_BYTES,
    SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
    SERIAL_FAILURE_STATUS_CODES,
    SERIAL_SUCCESS_STATUS_CODES,
    SERIAL_MIN_ACK_TIMEOUT,
)
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame
from mcubridge.protocol.protocol import (
    ACK_ONLY_COMMANDS,
    Status,
    expected_responses,
    response_to_request,
)
from mcubridge.protocol.structures import (
    AckPacket,
    PendingCommand,
)
from mcubridge.security.security import (
    generate_nonce_with_counter,
)
from mcubridge.services.handshake import SerialHandshakeFatal

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.serial")


class SerialTransport:
    """High-performance asyncio serial transport with flattened pipeline. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.service = service
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

        if self.service:
            self.service.register_serial_sender(self.send)

        self._stop_event = asyncio.Event()
        self._negotiating = False
        self._negotiation_future: asyncio.Future[bool] | None = None
        self._consecutive_crc_errors = 0
        self._tx_sequence_id = 0
        self._packet_semaphore = asyncio.Semaphore(16)
        self.is_connected = False

        self._current: PendingCommand | None = None
        self._flow_lock = asyncio.Lock()

        self._ack_timeout = max(self.config.serial_retry_timeout or 0, SERIAL_MIN_ACK_TIMEOUT)
        self._response_timeout = max(self.config.serial_response_timeout or 0, self._ack_timeout)
        self._max_attempts = max(1, self.config.serial_retry_attempts or 1)

    class _RetryableSerialError(Exception):
        """Marker exception to request another send attempt."""

    class _FatalSerialError(Exception):
        """Raised when a frame should not be retried."""

        def __init__(self, status: int | None) -> None:
            super().__init__(status)
            self.status = status

    def _active_transport(self) -> serialx.BaseSerialTransport:
        if self.writer is None or self.writer.is_closing():
            raise RuntimeError("Serial writer inactive")
        return cast(serialx.BaseSerialTransport, self.writer.transport)

    def _switch_local_baudrate(self, target_baud: int) -> None:
        try:
            self._active_transport().serial.baudrate = target_baud
            logger.info("Local UART switched to %d baud", target_baud)
        except (AttributeError, OSError, ValueError, serialx.SerialException) as e:
            raise RuntimeError(f"UART access failed: {e}") from e

    async def reset(self) -> None:
        async with self._flow_lock:
            if self._current:
                self._current.mark_failure(Status.TIMEOUT.value)
            self._current = None

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
            retry=tenacity.retry_if_not_exception_type((asyncio.CancelledError, SerialHandshakeFatal)),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        try:
            await retryer(self._connect_and_run)
        except asyncio.CancelledError:
            pass
        except SerialHandshakeFatal as exc:
            logger.error("Fatal serial handshake error: %s", exc)
            raise

    async def _connect_and_run(self) -> None:
        logger.info("Connecting to MCU on %s...", self.config.serial_port)
        connect_baud = self.config.serial_safe_baud or protocol.DEFAULT_SAFE_BAUDRATE
        await self._toggle_dtr(connect_baud)
        self.reader, self.writer = await serialx.open_serial_connection(
            url=self.config.serial_port, baudrate=connect_baud, xonxoff=False
        )
        self.state.serial_writer = cast(asyncio.BaseTransport, self.writer.transport)
        read_task = asyncio.get_running_loop().create_task(self._read_loop(self.reader))
        self.is_connected = True
        try:
            if self.config.serial_baud != connect_baud and not await self._negotiate_baudrate(self.config.serial_baud):
                raise ConnectionError("Baudrate negotiation failed")
            if self.service:
                await self.service.on_serial_connected()

            # Wait for either stop event or read task failure
            wait_stop = asyncio.create_task(self._stop_event.wait())
            done, _ = await asyncio.wait([wait_stop, read_task], return_when=asyncio.FIRST_COMPLETED)
            if not wait_stop.done():
                wait_stop.cancel()

            # If read_task finished first, it means connection was lost
            if read_task in done:
                raise ConnectionError("Serial connection lost")
        finally:
            self.is_connected = False
            read_task.cancel()
            with contextlib.suppress(asyncio.IncompleteReadError, asyncio.CancelledError):
                await read_task
            if self.service:
                with contextlib.suppress(Exception):
                    await self.service.on_serial_disconnected()
            if self.writer:
                self.writer.close()

    async def _toggle_dtr(self, baudrate: int) -> None:
        try:
            async with serialx.async_serial_for_url(
                self.config.serial_port, baudrate=baudrate, xonxoff=False
            ) as serial_port:
                await serial_port.set_modem_pins(dtr=False)
                await asyncio.sleep(0.1)
                await serial_port.set_modem_pins(dtr=True)
        except (serialx.SerialException, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Unable to toggle DTR on %s: %s", self.config.serial_port, exc)

    async def stop(self) -> None:
        self._stop_event.set()
        if self.writer:
            self.writer.close()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while not self._stop_event.is_set():
            try:
                packet_with_sep = await reader.readuntil(protocol.FRAME_DELIMITER)
                packet_view = memoryview(packet_with_sep)[:-1]
                if packet_view:
                    await self._process_packet(packet_view)
            except asyncio.LimitOverrunError:
                self.state.serial_decode_errors += 1
                await reader.read(MAX_SERIAL_FRAME_BYTES)
            except asyncio.IncompleteReadError:
                break
            except Exception as exc:
                logger.error("Error in _read_loop: %s", exc)
                break

    async def _process_packet(self, encoded_packet: bytes | memoryview) -> None:
        """Processes a packet from the serial stream. [FLATTENED]"""
        try:
            # Ensure we have bytes for cobs.decode
            raw_bytes = bytes(encoded_packet) if isinstance(encoded_packet, memoryview) else encoded_packet
            decoded = cobs.decode(raw_bytes)
            frame = Frame.parse(decoded, self.state.link_session_key if self.state.is_synchronized else None)
        except Exception as exc:
            logger.warning("[SERIAL <- MCU] [MALFORMED]: %s", exc)
            self.state.serial_decode_errors += 1
            await self._check_baudrate_fallback()
            return

        if self._negotiating and self._negotiation_future and not self._negotiation_future.done():
            if frame.command_id == protocol.Command.CMD_SET_BAUDRATE_RESP.value:
                self._switch_local_baudrate(self.config.serial_baud)
                self._negotiation_future.set_result(True)
                return

        cmd_id, seq_id, payload = frame.command_id, frame.sequence_id, frame.payload

        # Anti-replay validation
        is_excluded = (protocol.STATUS_CODE_MIN <= cmd_id <= protocol.STATUS_CODE_MAX) or (
            protocol.SYSTEM_COMMAND_MIN <= cmd_id <= protocol.SYSTEM_COMMAND_MAX
        )
        if self.state.is_synchronized and not is_excluded:
            from mcubridge.security.security import validate_nonce_counter

            ok, new_counter = validate_nonce_counter(frame.nonce, self.state.link_last_nonce_counter)
            if not ok:
                logger.warning("Anti-replay validation failed")
                return
            self.state.link_last_nonce_counter = new_counter

        # Correlation and Service dispatch
        self._correlate_frame(cmd_id, payload)
        if self.service:
            await self.service.handle_mcu_frame(cmd_id, seq_id, payload)

        self.state.metrics.serial_bytes_received.inc(len(encoded_packet))
        self.state.metrics.serial_frames_received.inc()

    def _correlate_frame(self, command_id: int, payload: bytes) -> None:
        pending = self._current
        if pending is None:
            return
        if command_id == Status.ACK.value:
            ack_target = pending.command_id
            if payload:
                with contextlib.suppress(Exception):
                    ack_target = AckPacket.decode(payload).command_id
            if ack_target == pending.command_id:
                pending.ack_received = True
                if not pending.expected_resp_ids:
                    pending.mark_success(payload)
            return
        if response_to_request(command_id) == pending.command_id:
            pending.mark_success(payload)
            return
        if command_id in SERIAL_FAILURE_STATUS_CODES:
            pending.mark_failure(command_id)
        elif command_id in SERIAL_SUCCESS_STATUS_CODES and not pending.expected_resp_ids:
            pending.mark_success(payload)

    async def _check_baudrate_fallback(self) -> None:
        self._consecutive_crc_errors += 1
        if self._consecutive_crc_errors >= self.config.serial_fallback_threshold:
            logger.warning("Fallback to %d baud", self.config.serial_safe_baud)
            self._consecutive_crc_errors = 0
            if self.config.serial_baud != self.config.serial_safe_baud:
                await self._negotiate_baudrate(self.config.serial_safe_baud)

    async def send(self, command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
        """Unified send method with automatic tracking and retries. [FLATTENED]"""
        if not self.writer or self.writer.is_closing():
            return False

        is_handshake = command_id in (protocol.Command.CMD_LINK_SYNC.value, protocol.Command.CMD_LINK_RESET.value)
        is_tracked = not is_handshake and (bool(expected_responses(command_id)) or command_id in ACK_ONLY_COMMANDS)

        if not is_tracked:
            return await self.send_raw(command_id, payload, seq_id)

        async with self._flow_lock:
            pending = PendingCommand(command_id=command_id, expected_resp_ids=set(expected_responses(command_id)))
            self._current = pending
            try:
                retryer = tenacity.AsyncRetrying(
                    stop=tenacity.stop_after_attempt(self._max_attempts),
                    wait=tenacity.wait_exponential(
                        multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE, max=SERIAL_HANDSHAKE_BACKOFF_MAX
                    ),
                    retry=tenacity.retry_if_exception_type(self._RetryableSerialError),
                    reraise=True,
                )
                async for attempt in retryer:
                    with attempt:
                        pending.attempts = (pending.attempts or 0) + 1
                        pending.ack_received = False
                        pending.success = None

                        if not await self.send_raw(command_id, payload):
                            raise self._FatalSerialError(None)

                        try:
                            async with asyncio.timeout(self._response_timeout):
                                await pending.completion.wait()
                                if pending.success:
                                    return True
                        except TimeoutError:
                            raise self._RetryableSerialError()

                        if pending.failure_status is not None:
                            raise self._FatalSerialError(pending.failure_status)
                        raise self._RetryableSerialError()
                return True
            except self._FatalSerialError as exc:
                pending.mark_failure(exc.status)
                return False
            except self._RetryableSerialError:
                pending.mark_failure(Status.TIMEOUT.value)
                return False
            finally:
                self._current = None

    async def send_and_wait_payload(self, command_id: int, payload: bytes) -> bytes | None:
        """Helper to send a command and return its response payload."""
        async with self._flow_lock:
            pending = PendingCommand(command_id=command_id, expected_resp_ids=set(expected_responses(command_id)))
            self._current = pending
            try:
                retryer = tenacity.AsyncRetrying(
                    stop=tenacity.stop_after_attempt(self._max_attempts),
                    wait=tenacity.wait_exponential(
                        multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE, max=SERIAL_HANDSHAKE_BACKOFF_MAX
                    ),
                    retry=tenacity.retry_if_exception_type(self._RetryableSerialError),
                    reraise=True,
                )
                async for attempt in retryer:
                    with attempt:
                        if not await self.send_raw(command_id, payload):
                            raise self._FatalSerialError(None)
                        try:
                            async with asyncio.timeout(self._response_timeout):
                                await pending.completion.wait()
                                if pending.success:
                                    return pending.response_payload
                        except TimeoutError:
                            raise self._RetryableSerialError()
                        raise self._RetryableSerialError()
            except Exception:
                return None
            finally:
                self._current = None
        return None

    async def send_raw(self, command_id: int, payload: bytes, seq_id: int | None = None) -> bool:
        """Low-level send logic without tracking."""
        if not self.writer:
            return False

        if not self.state.serial_tx_allowed.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(30.0):
                    await self.state.serial_tx_allowed.wait()

        if seq_id is None:
            self._tx_sequence_id = (self._tx_sequence_id + 1) & protocol.UINT16_MAX
            seq_id = self._tx_sequence_id

        is_excluded = (protocol.STATUS_CODE_MIN <= command_id <= protocol.STATUS_CODE_MAX) or (
            protocol.SYSTEM_COMMAND_MIN <= command_id <= protocol.SYSTEM_COMMAND_MAX
        )
        nonce = b"\x00" * protocol.AEAD_NONCE_SIZE
        if self.state.is_synchronized and not is_excluded:
            nonce, new_counter = generate_nonce_with_counter(self.state.link_nonce_counter)
            self.state.link_nonce_counter = new_counter

        frame = Frame(command_id=command_id, sequence_id=seq_id, payload=payload, nonce=nonce)
        encoded = (
            cobs.encode(frame.build(self.state.link_session_key if self.state.is_synchronized else None))
            + protocol.FRAME_DELIMITER
        )

        if logger.is_enabled_for(logging.DEBUG):
            logger.debug(
                "[SERIAL -> MCU] [CMD:0x%02X] [RAW]: [%s]",
                command_id,
                encoded.hex().upper(),
            )

        try:
            self.writer.write(encoded)
            await self.writer.drain()
            self.state.metrics.serial_bytes_sent.inc(len(encoded))
            self.state.metrics.serial_frames_sent.inc()
            return True
        except Exception as exc:
            logger.error("Serial write failed: %s", exc)
            return False

    async def _negotiate_baudrate(self, target_baud: int) -> bool:
        from mcubridge.protocol.structures import SetBaudratePacket

        payload = SetBaudratePacket(baudrate=target_baud).encode()
        self._negotiating = True
        try:
            self._negotiation_future = self.loop.create_future() if self.loop else None
            if not await self.send_raw(protocol.Command.CMD_SET_BAUDRATE.value, payload):
                return False
            if self._negotiation_future:
                await asyncio.wait_for(self._negotiation_future, timeout=SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT)
            return True
        except Exception:
            return False
        finally:
            self._negotiating = False

    async def acknowledge(self, command_id: int, seq_id: int, *, status: Status = Status.ACK) -> None:
        await self.send_raw(status.value, AckPacket(command_id=command_id).encode(), seq_id)
