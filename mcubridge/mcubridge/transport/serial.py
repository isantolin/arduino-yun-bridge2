"""Serial transport implementation using pyserial-asyncio-fast Streams.

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
import structlog
import time
from typing import TYPE_CHECKING, Any, cast, Callable

import msgspec
from cobs import cobs
import serial
import serial_asyncio_fast
from serial import SerialException
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
from mcubridge.protocol.frame import Frame, HEADER_STRUCT
from mcubridge.protocol.protocol import (
    ACK_ONLY_COMMANDS,
    RESPONSE_ONLY_COMMANDS,
    Status,
    expected_responses,
    response_to_request,
)
from mcubridge.protocol.structures import (
    AckPacket,
    PendingCommand,
    PipelineEvent,
)
from mcubridge.security.security import (
    aead_decrypt,
    aead_encrypt,
    generate_nonce_with_counter,
)
from mcubridge.services.handshake import SerialHandshakeFatal

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.serial")


class SerialTransport:
    """High-performance asyncio serial transport with flow control. [SIL-2]"""

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

        # Register ourselves as the sender for the service if provided
        if self.service:
            self.service.register_serial_sender(self.send)

        self._stop_event = asyncio.Event()
        self._negotiating = False
        self._negotiation_future: asyncio.Future[bool] | None = None
        self._consecutive_crc_errors = 0
        self._tx_sequence_id = 0
        self._packet_semaphore = asyncio.Semaphore(16)
        self.is_connected = False

        # Flow Control State
        self._current: PendingCommand | None = None
        self._flow_lock = asyncio.Lock()
        self._pipeline_observer: Callable[[PipelineEvent], None] | None = None

        self._ack_timeout = max(
            self.config.serial_retry_timeout or 0, SERIAL_MIN_ACK_TIMEOUT
        )
        self._response_timeout = max(
            self.config.serial_response_timeout or 0, self._ack_timeout
        )
        self._max_attempts = max(1, self.config.serial_retry_attempts or 1)

    class _RetryableSerialError(Exception):
        """Marker exception to request another send attempt."""

    class _FatalSerialError(Exception):
        """Raised when a frame should not be retried."""

        def __init__(self, status: int | None) -> None:
            super().__init__(status)
            self.status = status

    def set_pipeline_observer(
        self, observer: Callable[[PipelineEvent], None] | None
    ) -> None:
        self._pipeline_observer = observer

    def _switch_local_baudrate(self, target_baud: int) -> None:
        if self.writer is None or self.writer.is_closing():
            raise RuntimeError("Serial writer inactive")
        try:
            serial_port = cast(Any, self.writer.transport).serial
            serial_port.baudrate = target_baud
            logger.info("Local UART switched to %d baud", target_baud)
        except (AttributeError, ValueError) as e:
            raise RuntimeError(f"UART access failed: {e}") from e

    async def reset(self) -> None:
        """Reset flow control state (e.g. on link drop)."""
        async with self._flow_lock:
            if self._current and not self._current.completion.is_set():
                self._current.mark_failure(Status.TIMEOUT.value)
                self._notify_pipeline(
                    "abandoned", self._current, status=Status.TIMEOUT.value
                )
            self._current = None

    async def run(self) -> None:
        """Main transport entry point with auto-reconnect."""
        self.loop = asyncio.get_running_loop()
        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
            retry=tenacity.retry_if_not_exception_type(
                (asyncio.CancelledError, SerialHandshakeFatal)
            ),
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
        """Single connection attempt and execution."""
        logger.info("Connecting to MCU on %s...", self.config.serial_port)
        await self._toggle_dtr()
        connect_baud = self.config.serial_safe_baud or protocol.DEFAULT_SAFE_BAUDRATE
        self.reader, self.writer = await serial_asyncio_fast.open_serial_connection(
            url=self.config.serial_port,
            baudrate=connect_baud,
            xonxoff=False,
        )
        self.state.serial_writer = cast(asyncio.BaseTransport, self.writer)
        read_task = asyncio.get_running_loop().create_task(self._read_loop(self.reader))
        self.is_connected = True
        try:
            if self.config.serial_baud != connect_baud:
                if not await self._negotiate_baudrate(self.config.serial_baud):
                    raise ConnectionError("Baudrate negotiation failed")
            if self.service:
                await self.service.on_serial_connected()
            stop_task = asyncio.get_running_loop().create_task(self._stop_event.wait())
            done, pending = await asyncio.wait(
                [read_task, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if read_task in done:
                if exc := read_task.exception():
                    raise exc
                raise ConnectionError("Serial connection lost (EOF)")
        finally:
            self.is_connected = False
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await read_task
            try:
                if self.service:
                    await self.service.on_serial_disconnected()
            except (asyncio.CancelledError, OSError, ValueError, RuntimeError) as e:
                logger.error("Error in on_serial_disconnected hook: %s", e)
            if self.writer:
                self.writer.close()

    async def _toggle_dtr(self) -> None:
        """Hardware reset via DTR toggle."""
        try:

            def _pulse() -> None:
                with serial.Serial(self.config.serial_port) as s:
                    s.dtr = False
                    time.sleep(0.1)
                    s.dtr = True

            await asyncio.get_running_loop().run_in_executor(None, _pulse)
        except (SerialException, OSError):
            pass

    async def stop(self) -> None:
        self._stop_event.set()
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while not self._stop_event.is_set():
            try:
                packet_with_sep = await reader.readuntil(protocol.FRAME_DELIMITER)
                packet_view = memoryview(packet_with_sep)[:-1]
                if packet_view:
                    self._process_packet(packet_view)
            except asyncio.LimitOverrunError:
                self.state.serial_decode_errors += 1
                self.state.metrics.serial_decode_errors.inc()
                await reader.read(MAX_SERIAL_FRAME_BYTES)
            except asyncio.IncompleteReadError:
                break
            except (asyncio.CancelledError, OSError, ValueError, RuntimeError) as exc:
                logger.error("Error in _read_loop: %s", exc)
                break

    def _process_packet(self, encoded_packet: bytes | memoryview) -> None:
        if (
            self._negotiating
            and self._negotiation_future
            and not self._negotiation_future.done()
        ):
            try:
                frame = Frame.parse(cobs.decode(encoded_packet))
                if frame.command_id == protocol.Command.CMD_SET_BAUDRATE_RESP.value:
                    self._switch_local_baudrate(self.config.serial_baud)
                    self._negotiation_future.set_result(True)
                    return
            except (asyncio.CancelledError, OSError, ValueError, RuntimeError):
                pass
        if self.loop:
            self.loop.create_task(self._async_process_packet_with_limit(encoded_packet))

    async def _async_process_packet_with_limit(
        self, encoded_packet: bytes | memoryview
    ) -> None:
        async with self._packet_semaphore:
            await self._async_process_packet(encoded_packet)

    async def _async_process_packet(self, encoded_packet: bytes | memoryview) -> None:
        packet_bytes = (
            encoded_packet
            if isinstance(encoded_packet, bytes)
            else encoded_packet.tobytes()
        )
        if logger.is_enabled_for(logging.DEBUG):
            logger.debug("[SERIAL <- MCU] [RAW]: [%s]", packet_bytes.hex(" ").upper())
        try:
            decoded = cobs.decode(packet_bytes)
            frame = Frame.parse(decoded)
            cmd_id, seq_id, payload = frame.command_id, frame.sequence_id, frame.payload

            is_excluded = (
                protocol.STATUS_CODE_MIN <= cmd_id <= protocol.STATUS_CODE_MAX
                or protocol.SYSTEM_COMMAND_MIN <= cmd_id <= protocol.SYSTEM_COMMAND_MAX
            )

            if (
                self.state.is_synchronized
                and self.state.link_session_key
                and not is_excluded
            ):
                try:
                    # [SIL-2] Zero-Wrapper AEAD: Use preserved raw header bytes directly as AD
                    payload = aead_decrypt(
                        self.state.link_session_key,
                        frame.nonce,
                        payload + frame.tag,
                        frame.header_bytes,
                    )
                except (asyncio.CancelledError, OSError, ValueError, RuntimeError) as e:
                    raise ValueError(f"AEAD Authentication Failed: {e}") from e

            # Correlate with flow control
            self._correlate_frame(cmd_id, payload)

            if self.service:
                await self.service.handle_mcu_frame(cmd_id, seq_id, payload)

            self.state.metrics.serial_bytes_received.inc(len(encoded_packet))
            self.state.metrics.serial_frames_received.inc()
            self.state.serial_throughput_stats.record_rx(len(encoded_packet))

        except (cobs.DecodeError, ValueError, msgspec.DecodeError) as exc:
            logger.warning(
                "[SERIAL <- MCU] [MALFORMED (ERR: %s)]: [%s]",
                exc,
                packet_bytes.hex(" ").upper(),
            )
            self.state.serial_decode_errors += 1
            self.state.metrics.serial_decode_errors.inc()
            await self._check_baudrate_fallback()
        except (asyncio.CancelledError, OSError, ValueError, RuntimeError) as exc:
            logger.error(
                "[SERIAL <- MCU] [ERROR (ERR: %s)]: [%s]",
                exc,
                packet_bytes.hex(" ").upper(),
            )
            self.state.serial_decode_errors += 1
            self.state.metrics.serial_decode_errors.inc()

    def _correlate_frame(self, command_id: int, payload: bytes) -> None:
        pending = self._current
        if pending is None:
            return

        if command_id == Status.ACK.value:
            ack_target = pending.command_id
            if payload:
                with contextlib.suppress(Exception):
                    ack_target = msgspec.msgpack.decode(
                        payload, type=AckPacket
                    ).command_id
            if ack_target != pending.command_id:
                return
            if not pending.ack_received:
                pending.ack_received = True
                self._notify_pipeline("ack", pending)
            if not pending.expected_resp_ids:
                pending.mark_success(payload)
            return

        if response_to_request(command_id) == pending.command_id:
            pending.mark_success(payload)
            return

        if command_id in SERIAL_FAILURE_STATUS_CODES:
            reject = not payload or (
                not payload.isascii()
                if not payload.startswith(b"\x91")
                else msgspec.msgpack.decode(payload, type=AckPacket).command_id
                == pending.command_id
            )
            if reject:
                pending.mark_failure(command_id)
            return

        if command_id in SERIAL_SUCCESS_STATUS_CODES and not pending.expected_resp_ids:
            pending.mark_success(payload)

    async def _check_baudrate_fallback(self) -> None:
        self._consecutive_crc_errors += 1
        if self._consecutive_crc_errors >= self.config.serial_fallback_threshold:
            logger.warning(
                "CRC error threshold reached. Fallback to %d",
                self.config.serial_safe_baud,
            )
            self._consecutive_crc_errors = 0
            if self.config.serial_baud != self.config.serial_safe_baud:
                await self._negotiate_baudrate(self.config.serial_safe_baud)

    async def send(
        self, command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        if not self.writer or self.writer.is_closing():
            return False

        # Trackable command logic
        is_handshake = command_id in (
            protocol.Command.CMD_LINK_SYNC.value,
            protocol.Command.CMD_LINK_RESET.value,
        )
        if not is_handshake and (
            bool(expected_responses(command_id)) or command_id in ACK_ONLY_COMMANDS
        ):
            return await self._send_tracked(command_id, payload)

        return await self._send_raw(command_id, payload, seq_id)

    async def _send_tracked(self, command_id: int, payload: bytes) -> bool:
        pending = PendingCommand(
            command_id=command_id, expected_resp_ids=set(expected_responses(command_id))
        )
        async with self._flow_lock:
            while self._current is not None:
                await asyncio.sleep(0.01)
            self._current = pending
        try:
            retryer = tenacity.AsyncRetrying(
                stop=tenacity.stop_after_attempt(self._max_attempts),
                wait=tenacity.wait_exponential(
                    multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE,
                    max=SERIAL_HANDSHAKE_BACKOFF_MAX,
                ),
                retry=tenacity.retry_if_exception_type(self._RetryableSerialError),
                reraise=True,
            )
            async for attempt in retryer:
                with attempt:
                    pending.attempts = (pending.attempts or 0) + 1
                    self._notify_pipeline("start", pending)
                    pending.completion.clear()
                    pending.ack_received = False
                    pending.success = None
                    pending.failure_status = None

                    if not await self._send_raw(command_id, payload):
                        pending.mark_failure(None)
                        raise self._FatalSerialError(None)

                    self._notify_pipeline("sent", pending)
                    expect_ack = command_id not in RESPONSE_ONLY_COMMANDS
                    try:
                        async with asyncio.timeout(self._response_timeout):
                            if expect_ack and not pending.ack_received:
                                await pending.completion.wait()
                                if pending.success:
                                    return True
                            if not pending.success:
                                await pending.completion.wait()
                    except asyncio.TimeoutError:
                        raise self._RetryableSerialError()

                    if pending.success:
                        self._notify_pipeline("success", pending)
                        return True
                    if pending.failure_status is not None:
                        raise self._FatalSerialError(pending.failure_status)
                    raise self._RetryableSerialError()
            return True
        except (asyncio.CancelledError, OSError, ValueError, RuntimeError) as e:
            st = getattr(e, "status", Status.TIMEOUT.value)
            pending.mark_failure(st)
            self._notify_pipeline("failure", pending, status=st)
            return False
        finally:
            async with self._flow_lock:
                self._current = None

    async def send_and_wait_payload(
        self, command_id: int, payload: bytes
    ) -> bytes | None:
        pending = PendingCommand(
            command_id=command_id, expected_resp_ids=set(expected_responses(command_id))
        )
        async with self._flow_lock:
            while self._current is not None:
                await asyncio.sleep(0.01)
            self._current = pending
        try:
            ok = await self._send_tracked_internal(pending, payload)
            return pending.response_payload if ok else None
        finally:
            async with self._flow_lock:
                self._current = None

    async def _send_tracked_internal(
        self, pending: PendingCommand, payload: bytes
    ) -> bool:
        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(self._max_attempts),
            wait=tenacity.wait_exponential(
                multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE,
                max=SERIAL_HANDSHAKE_BACKOFF_MAX,
            ),
            retry=tenacity.retry_if_exception_type(self._RetryableSerialError),
            reraise=True,
        )
        async for attempt in retryer:
            with attempt:
                pending.attempts = (pending.attempts or 0) + 1
                self._notify_pipeline("start", pending)
                pending.completion.clear()
                pending.ack_received = False
                pending.success = None
                pending.failure_status = None
                if not await self._send_raw(pending.command_id, payload):
                    raise self._FatalSerialError(None)
                self._notify_pipeline("sent", pending)
                expect_ack = pending.command_id not in RESPONSE_ONLY_COMMANDS
                try:
                    async with asyncio.timeout(self._response_timeout):
                        if expect_ack and not pending.ack_received:
                            await pending.completion.wait()
                            if pending.success:
                                return True
                        if not pending.success:
                            await pending.completion.wait()
                except asyncio.TimeoutError:
                    raise self._RetryableSerialError()
                if pending.success:
                    return True
                if pending.failure_status is not None:
                    raise self._FatalSerialError(pending.failure_status)
                raise self._RetryableSerialError()
        return False

    async def send_raw(
        self, command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        """Send a raw frame without tracking or ACK wait (e.g. system frames)."""
        return await self._send_raw(command_id, payload, seq_id)

    async def _send_raw(
        self, command_id: int, payload: bytes, seq_id: int | None = None
    ) -> bool:
        if not self.writer:
            return False
        if not self.state.serial_tx_allowed.is_set():
            try:
                async with asyncio.timeout(30.0):
                    await self.state.serial_tx_allowed.wait()
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for serial TX allowed")
                return False
            except (asyncio.CancelledError, OSError, ValueError, RuntimeError) as exc:
                logger.error("Unexpected error waiting for TX: %s", exc)
                return False

        if seq_id is None:
            self._tx_sequence_id = (self._tx_sequence_id + 1) & protocol.UINT16_MAX
            seq_id = self._tx_sequence_id

        is_excluded = (
            protocol.STATUS_CODE_MIN <= command_id <= protocol.STATUS_CODE_MAX
            or protocol.SYSTEM_COMMAND_MIN <= command_id <= protocol.SYSTEM_COMMAND_MAX
        )

        if (
            self.state.is_synchronized
            and self.state.link_session_key
            and not is_excluded
        ):
            nonce, new_counter = generate_nonce_with_counter(
                self.state.link_nonce_counter
            )
            self.state.link_nonce_counter = new_counter

            header_bytes = HEADER_STRUCT.pack(
                protocol.PROTOCOL_VERSION, len(payload), int(command_id), seq_id
            )
            encrypted_blob = aead_encrypt(
                self.state.link_session_key, nonce, payload, header_bytes
            )
            frame = Frame(
                command_id=command_id,
                sequence_id=seq_id,
                payload=encrypted_blob[:-16],
                nonce=nonce,
                tag=encrypted_blob[-16:],
            )
        else:
            frame = Frame(command_id=command_id, sequence_id=seq_id, payload=payload)

        encoded = cobs.encode(frame.build()) + protocol.FRAME_DELIMITER
        if logger.is_enabled_for(logging.DEBUG):
            logger.debug(
                "[SERIAL -> MCU] [CMD:0x%02X] [RAW]: [%s]",
                command_id,
                encoded.hex(" ").upper(),
            )
        self.writer.write(encoded)
        await self.writer.drain()
        self.state.metrics.serial_bytes_sent.inc(len(encoded))
        self.state.metrics.serial_frames_sent.inc()
        self.state.serial_throughput_stats.record_tx(len(encoded))
        return True

    async def _negotiate_baudrate(self, target_baud: int) -> bool:
        from mcubridge.protocol.structures import SetBaudratePacket

        payload = msgspec.msgpack.encode(SetBaudratePacket(baudrate=target_baud))
        self._negotiating = True
        try:
            self._negotiation_future = self.loop.create_future() if self.loop else None
            if not await self._send_raw(
                protocol.Command.CMD_SET_BAUDRATE.value, payload
            ):
                return False
            (
                await asyncio.wait_for(
                    self._negotiation_future,
                    timeout=SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
                )
                if self._negotiation_future
                else None
            )
            await asyncio.sleep(0.1)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._negotiating = False

    def _notify_pipeline(
        self, event: str, pending: PendingCommand, *, status: int | None = None
    ) -> None:
        if self._pipeline_observer:
            self._pipeline_observer(
                PipelineEvent(
                    event=event,
                    command_id=pending.command_id,
                    attempt=max(1, pending.attempts or 1),
                    ack_received=pending.ack_received,
                    status=status,
                    timestamp=time.time(),
                )
            )

    async def acknowledge(
        self, command_id: int, seq_id: int, *, status: Status = Status.ACK
    ) -> None:
        payload = msgspec.msgpack.encode(AckPacket(command_id=command_id))
        await self._send_raw(status.value, payload)
