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
from mcubridge.protocol import mcubridge_pb2 as pb

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from cobs import cobs
import serialx
import structlog
import tenacity
from google.protobuf.message import Message as ProtobufMessage, DecodeError as ProtobufDecodeError

from mcubridge.config.const import (
    MAX_SERIAL_FRAME_BYTES,
    SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
    SERIAL_FAILURE_STATUS_CODES,
    SERIAL_SUCCESS_STATUS_CODES,
    SERIAL_MIN_ACK_TIMEOUT,
)
from mcubridge.protocol import protocol, is_system_command
from mcubridge.protocol.protocol import (
    ACK_ONLY_COMMANDS,
    Status,
    expected_responses,
    response_to_request,
)
from mcubridge.protocol.structures import (
    PendingCommand,
)
from mcubridge.security.security import (
    generate_nonce_with_counter,
)
from mcubridge.services.handshake import SerialHandshakeFatal

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState
    from mcubridge.services.runtime import BridgeService

logger = structlog.get_logger("mcubridge.serial")


class SerialTransport:
    """High-performance asyncio serial transport with flattened pipeline. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: BridgeService | None,
    ) -> None:
        self.config = config
        self.state = state
        self.service = service
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

        self._stop_event = asyncio.Event()
        self._negotiating = False
        self._negotiation_future: asyncio.Future[bool] | None = None
        self._consecutive_crc_errors = 0
        self._tx_sequence_id = 0

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
        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
            retry=tenacity.retry_if_not_exception_type((asyncio.CancelledError, SerialHandshakeFatal)),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        try:
            await retryer(self._connect_and_run)
        except asyncio.CancelledError:
            logger.info("Serial transport cancelled")
        except SerialHandshakeFatal as exc:
            logger.error("Fatal serial handshake error: %s", exc)
            raise

    async def _connect_and_run(self) -> None:
        logger.info("Connecting to MCU on %s...", self.config.serial_port)
        connect_baud = self.config.serial_safe_baud or protocol.DEFAULT_SAFE_BAUDRATE
        self.reader, self.writer = await serialx.open_serial_connection(
            url=self.config.serial_port, baudrate=connect_baud, xonxoff=False
        )
        self.state.serial_writer = cast(asyncio.BaseTransport, self.writer.transport)
        await self._toggle_dtr()
        read_task = asyncio.get_running_loop().create_task(self._read_loop(self.reader))
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
            read_task.cancel()
            try:
                await read_task
            except (asyncio.IncompleteReadError, asyncio.CancelledError):
                logger.debug("Serial read task cancelled or incomplete during cleanup")
            if self.service:
                try:
                    await self.service.on_serial_disconnected()
                except (OSError, RuntimeError, ValueError, TypeError) as e:
                    logger.warning("Error during serial disconnect cleanup", error=e)
            if self.writer:
                self.writer.close()

    async def _toggle_dtr(self) -> None:
        try:
            serial_obj = self._active_transport().serial
            serial_obj.dtr = False
            await asyncio.sleep(0.1)
            serial_obj.dtr = True
        except (AttributeError, OSError, ValueError, serialx.SerialException, RuntimeError) as exc:
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
            except (OSError, RuntimeError, ValueError, TypeError, serialx.SerialException) as exc:
                logger.error("Error in _read_loop: %s", exc)
                break

    async def _process_packet(self, encoded_packet: bytes | memoryview) -> None:
        """Processes a packet from the serial stream. [FLATTENED] [SIL-2]"""
        from mcubridge.protocol.frame import parse_frame

        try:
            # Ensure we have bytes for cobs.decode
            raw_bytes = bytes(encoded_packet) if isinstance(encoded_packet, memoryview) else encoded_packet
            decoded = cobs.decode(raw_bytes)
            decoded_frame = parse_frame(decoded, self.state.link_session_key if self.state.is_synchronized else None)
        except (cobs.DecodeError, ValueError, TypeError, RuntimeError) as exc:
            logger.warning("[SERIAL <- MCU] [MALFORMED]: %s", exc)
            self.state.serial_decode_errors += 1
            await self._check_baudrate_fallback()
            return

        envelope = decoded_frame.envelope
        payload = decoded_frame.payload
        cmd_id, seq_id = envelope.command_id, envelope.sequence_id

        if self._negotiating and self._negotiation_future and not self._negotiation_future.done():
            if cmd_id == protocol.Command.CMD_SET_BAUDRATE_RESP.value:
                self._switch_local_baudrate(self.config.serial_baud)
                self._negotiation_future.set_result(True)
                return

        # Anti-replay validation
        is_excluded = (protocol.STATUS_CODE_MIN <= cmd_id <= protocol.STATUS_CODE_MAX) or (
            protocol.SYSTEM_COMMAND_MIN <= cmd_id <= protocol.SYSTEM_COMMAND_MAX
        )
        if self.state.is_synchronized and not is_excluded:
            from mcubridge.security.security import validate_nonce_counter

            ok, new_counter = validate_nonce_counter(envelope.nonce, self.state.link_last_nonce_counter)
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

    def _correlate_frame(self, command_id: int, payload: bytes | ProtobufMessage) -> None:
        pending = self._current
        if pending is None:
            return
        if command_id == Status.ACK.value:
            ack_target = pending.command_id
            if payload:
                try:
                    if isinstance(payload, ProtobufMessage):
                        ack_target = getattr(payload, "command_id", ack_target)
                    else:
                        ack_target = pb.AckPacket.FromString(payload).command_id
                except (ProtobufDecodeError, TypeError, ValueError) as e:
                    logger.warning("Failed to decode MCU ACK payload", error=e)
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

    async def send(
        self, command_id: int, payload: bytes | ProtobufMessage, seq_id: int | None = None
    ) -> bool | bytes | ProtobufMessage:
        """Unified send method with automatic tracking, retries, and optional response return. [FLATTENED]"""
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
                                    return pending.response_payload if pending.response_payload is not None else True
                        except TimeoutError:
                            raise self._RetryableSerialError()

                        if pending.failure_status is not None:
                            raise self._FatalSerialError(pending.failure_status)
                        raise self._RetryableSerialError()
                return True
            except self._FatalSerialError as exc:
                pending.mark_failure(exc.status)
                return False
            except (self._RetryableSerialError, tenacity.RetryError):
                pending.mark_failure(Status.TIMEOUT.value)
                return False
            finally:
                self._current = None

    async def send_raw(self, command_id: int, payload: bytes | ProtobufMessage, seq_id: int | None = None) -> bool:
        """Low-level send logic without tracking."""
        if not self.writer:
            return False

        if not self.state.serial_tx_allowed.is_set():
            try:
                async with asyncio.timeout(30.0):
                    await self.state.serial_tx_allowed.wait()
            except TimeoutError:
                logger.warning("Timed out waiting for serial TX flow control")

        if seq_id is None:
            self._tx_sequence_id = (self._tx_sequence_id + 1) & protocol.UINT16_MAX
            seq_id = self._tx_sequence_id

        is_excluded = is_system_command(command_id)
        nonce = b"\x00" * protocol.AEAD_NONCE_SIZE
        if self.state.is_synchronized and not is_excluded:
            nonce, new_counter = generate_nonce_with_counter(self.state.link_nonce_counter)
            self.state.link_nonce_counter = new_counter

        from mcubridge.protocol.frame import build_frame

        encoded = (
            cobs.encode(
                build_frame(
                    command_id=command_id,
                    sequence_id=seq_id,
                    payload=payload,
                    nonce=nonce,
                    session_key=self.state.link_session_key if self.state.is_synchronized else None,
                )
            )
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
        except (AttributeError, OSError, RuntimeError, ValueError, serialx.SerialException) as exc:
            logger.error("Serial write failed: %s", exc)
            return False

    async def _negotiate_baudrate(self, target_baud: int) -> bool:
        payload = pb.SetBaudratePacket(baudrate=target_baud)
        self._negotiating = True
        try:
            self._negotiation_future = asyncio.get_running_loop().create_future()
            if not await self.send_raw(protocol.Command.CMD_SET_BAUDRATE.value, payload):
                return False
            if self._negotiation_future:
                await asyncio.wait_for(self._negotiation_future, timeout=SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT)
            return True
        except (asyncio.TimeoutError, OSError, RuntimeError, ValueError, serialx.SerialException) as exc:
            logger.error("Baudrate negotiation failed: %s", exc)
            return False
        finally:
            self._negotiating = False

    async def acknowledge(self, command_id: int, seq_id: int, *, status: Status = Status.ACK) -> None:
        await self.send_raw(status.value, pb.AckPacket(command_id=command_id), seq_id)
