"""Serial transport implementation using Google Pigweed HDLC and pyserial.

This module implements a Zero-Overhead asyncio transport using pyserial and
Google Pigweed HDLC Framing (pw_hdlc).

[SIL-2 COMPLIANCE]
- Deterministic buffer handling.
- Explicit lifecycle management.
- Zero dynamic allocation after initialization.
- Sequential command-response flow control.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Final, Protocol, cast

import anyio
import serial
from serial.urlhandler import protocol_socket
import structlog
import tenacity
from google.protobuf.message import DecodeError as ProtobufDecodeError, Message as ProtobufMessage
from pw_hdlc.decode import FrameDecoder
from pw_rpc import callback_client, packets as pw_packets
from pw_rpc.client import Client as PwRpcClient
from pw_rpc.descriptors import Channel as PwRpcChannel
from pw_rpc.internal.packet_pb2 import PacketType
from pw_tokenizer import detokenize

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    FLOW_CONTROL_WAIT_TIMEOUT_SECONDS,
    SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
    SERIAL_FAILURE_STATUS_CODES,
    SERIAL_HANDSHAKE_BACKOFF_BASE,
    SERIAL_HANDSHAKE_BACKOFF_MAX,
    SERIAL_SUCCESS_STATUS_CODES,
    Status,
    get_operation,
)
from mcubridge.protocol.structures import PendingCommand
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.serial")

_LEGACY_RAW_REQUESTS: Final[frozenset[int]] = frozenset(
    {
        protocol.Command.CMD_LINK_RESET.value,
        protocol.Command.CMD_SET_BAUDRATE.value,
        protocol.Command.CMD_FILE_READ.value,
    }
)

# Register wifi:// and tcp:// scheme aliases in pyserial
sys.modules["serial.urlhandler.protocol_wifi"] = protocol_socket
sys.modules["serial.urlhandler.protocol_tcp"] = protocol_socket


class SerialBridgeService(Protocol):
    """Protocol for bridge service callbacks invoked by SerialTransport."""

    async def on_serial_connected(self) -> None: ...

    async def on_serial_disconnected(self) -> None: ...

    async def handle_mcu_frame(
        self, operation_key: int | tuple[int, int], seq_id: int, payload: bytes | ProtobufMessage, /
    ) -> Any: ...


def resolve_serial_url(port_str: str) -> str:
    """Resolve serial device or network endpoint string into a canonical serial URL. [SIL-2]"""
    if not port_str:
        return ""
    clean = port_str.strip()
    if clean.startswith(("tcp://", "wifi://")):
        _, _, host_port = clean.partition("://")
        if ":" not in host_port:
            return f"socket://{host_port}:9000"
        return f"socket://{host_port}"
    if clean.startswith("socket://"):
        _, _, host_port = clean.partition("://")
        if ":" not in host_port:
            return f"socket://{host_port}:9000"
        return clean
    if ":" in clean and not clean.startswith(("/", ".")):
        return f"socket://{clean}"
    return clean


class SerialTransport:
    """High-performance asyncio serial/wireless transport with flattened pipeline. [SIL-2]"""

    config: RuntimeConfig
    state: RuntimeState
    service: SerialBridgeService | None
    serial: serial.Serial | None
    stop_event: asyncio.Event

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: SerialBridgeService | None,
    ) -> None:
        self.config = config
        self.state = state
        self.service: SerialBridgeService | None = service
        self.serial: serial.Serial | None = None

        self.stop_event = asyncio.Event()
        self.negotiation_future: asyncio.Future[bool] | None = None
        self.negotiation_target_baudrate: int | None = None
        self.consecutive_crc_errors = 0
        self.tx_sequence_id = 0

        self.current_command: PendingCommand | None = None
        self._flow_lock = asyncio.Lock()
        self._serial_write_lock = asyncio.Lock()
        self._rpc_write_tasks: set[asyncio.Task[bool]] = set()
        self._active_rpc_calls: list[Any] = []
        self.rpc_client = PwRpcClient(
            callback_client.Impl(),
            [PwRpcChannel(protocol.HDLC_ADDRESS_RPC, self._queue_rpc_packet)],
            protocol.PW_SERVICES,
        )

        self._ack_timeout = max(self.config.serial_retry_timeout or 0, float(protocol.DEFAULT_ACK_TIMEOUT_MS) / 1000.0)
        self._response_timeout = max(self.config.serial_response_timeout or 0, self._ack_timeout)
        self.max_attempts = max(1, self.config.serial_retry_attempts or 1)
        self.decoder = FrameDecoder()
        self.detokenizer = detokenize.Detokenizer()

    class _RetryableSerialError(Exception):
        """Marker exception to request another send attempt."""

    class _FatalSerialError(Exception):
        """Raised when a frame should not be retried."""

        def __init__(self, status: int | None) -> None:
            super().__init__(status)
            self.status = status

    def switch_local_baudrate(self, target_baud: int) -> None:
        try:
            if self.serial:
                self.serial.baudrate = target_baud
                logger.info("Local UART switched baudrate", baud=target_baud)
        except (AttributeError, OSError, ValueError) as e:
            raise RuntimeError(f"UART access failed: {e}") from e

    async def reset(self) -> None:
        async with self._flow_lock:
            if self.current_command:
                self.current_command.mark_failure(Status.TIMEOUT.value)
            self.current_command = None
        for call in tuple(self._active_rpc_calls):
            try:
                call.cancel()
            except (AttributeError, RuntimeError, ValueError) as exc:
                logger.debug("Unable to cancel pending Pigweed RPC during reset", error=str(exc))
        self._active_rpc_calls.clear()

    async def run(self) -> None:
        """Main lifecycle loop of the serial transport."""
        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
            retry=tenacity.retry_if_not_exception_type((asyncio.CancelledError, SystemExit, SerialHandshakeFatal)),
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        try:
            await retryer(self.connect_and_run)
        except (asyncio.CancelledError, SystemExit) as exc:
            logger.info("Serial transport lifecycle terminated", reason=type(exc).__name__)
        except SerialHandshakeFatal:
            raise

    async def connect_and_run(self) -> None:
        url = resolve_serial_url(self.config.serial_port)
        is_network = url.startswith(("socket://", "tcp://", "wifi://", "rfc2217://"))
        connect_baud = self.config.serial_safe_baud or protocol.DEFAULT_SAFE_BAUDRATE

        if is_network:
            logger.info("Connecting to MCU via network socket", url=url)
        else:
            logger.info("Connecting to MCU via serial port", port=self.config.serial_port)

        try:
            self.serial = await asyncio.to_thread(
                serial.serial_for_url,
                url,
                baudrate=connect_baud,
                timeout=0.1,
                write_timeout=2.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            self.state.serial_writer = self.serial
            if not is_network:
                await self.toggle_dtr()

            try:
                async with asyncio.TaskGroup() as tg:
                    serial_dev = self.serial
                    read_task = tg.create_task(self.read_loop(serial_dev), name="mcubridge-serial-read")
                    stop_task = tg.create_task(self.stop_event.wait(), name="mcubridge-serial-stop")

                    if self.service:
                        await self.service.on_serial_connected()

                    if (
                        not is_network
                        and self.config.serial_baud != connect_baud
                        and not await self.negotiate_baudrate(self.config.serial_baud)
                    ):
                        raise ConnectionError("Baudrate negotiation failed")

                    await asyncio.wait(
                        (read_task, stop_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    read_task.cancel()
                    stop_task.cancel()

                if not self.stop_event.is_set():
                    if is_network:
                        raise ConnectionError("Wireless network connection lost")
                    raise ConnectionError("Serial connection lost")
            except BaseExceptionGroup as eg:
                if len(eg.exceptions) == 1:
                    raise eg.exceptions[0] from None
                raise
            finally:
                if self.service:
                    with anyio.CancelScope(shield=True):
                        try:
                            await self.service.on_serial_disconnected()
                        except (OSError, RuntimeError, ValueError, TypeError) as e:
                            logger.error("Error during transport disconnect cleanup", error=str(e))
        finally:
            self.state.serial_writer = None
            if self.serial:
                try:
                    self.serial.cancel_read()
                    self.serial.cancel_write()
                except (AttributeError, OSError) as exc:
                    logger.debug("Failed to cancel pending serial I/O during cleanup", error=str(exc))
                try:
                    await asyncio.to_thread(self.serial.close)
                except (AttributeError, OSError) as exc:
                    logger.error("Failed to close serial port during cleanup", error=str(exc))
                self.serial = None

    async def toggle_dtr(self) -> None:
        try:
            if self.serial:

                def _dtr_pulse() -> None:
                    if self.serial:
                        self.serial.dtr = False
                        time.sleep(0.1)
                        self.serial.dtr = True
                        time.sleep(1.8)

                await asyncio.to_thread(_dtr_pulse)
        except (AttributeError, OSError, ValueError, RuntimeError) as exc:
            logger.error("Unable to toggle DTR", port=self.config.serial_port, error=str(exc))

    async def stop(self) -> None:
        self.stop_event.set()
        if self.serial:
            try:
                self.serial.cancel_read()
                self.serial.cancel_write()
            except (AttributeError, OSError) as exc:
                logger.debug("Failed to cancel pending serial I/O on stop", error=str(exc))
            try:
                await asyncio.to_thread(self.serial.close)
            except (AttributeError, OSError) as exc:
                logger.error("Failed to close serial port on stop", error=str(exc))

    async def feed_data(self, data: bytes | memoryview) -> None:
        """Feed incoming serial bytes into the HDLC frame decoder and dispatch valid frames."""
        raw_bytes = bytes(data) if isinstance(data, memoryview) else data
        for frame in self.decoder.process(raw_bytes):
            if not frame.ok():
                self.state.serial_decode_errors += 1
                await self.check_baudrate_fallback()
                continue
            await self.process_hdlc_frame(frame)

    async def read_loop(self, ser: serial.Serial) -> None:
        while not self.stop_event.is_set():
            try:
                packet_with_sep = await asyncio.to_thread(
                    ser.read_until, protocol.FRAME_DELIMITER, protocol.MAX_SERIAL_FRAME_BYTES
                )
                if packet_with_sep:
                    await self.feed_data(packet_with_sep)
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                if self.stop_event.is_set():
                    logger.debug("Serial connection closed during shutdown", error=str(exc))
                else:
                    logger.error("Error in serial read loop", error=str(exc))
                break

    async def process_hdlc_frame(self, frame: Any) -> None:
        """Processes a validated HDLC frame received from the serial link. [SIL-2]"""
        raw_len = len(frame.raw_encoded) if hasattr(frame, "raw_encoded") else len(frame.data)
        self.state.metrics.serial_bytes_received.inc(raw_len)
        self.state.metrics.serial_frames_received.inc()
        frame_data = bytes(frame.data)

        if self.config.serial_shared_secret:
            cipher = self.state.link_cipher
            if cipher is None:
                if frame.address != protocol.HDLC_ADDRESS_RPC:
                    logger.debug("Dropping unprotected pre-handshake HDLC frame", address=frame.address)
                    return
            else:
                try:
                    frame_data = cipher.decrypt(frame.address, frame_data)
                except ValueError as exc:
                    self.state.serial_decode_errors += 1
                    logger.error("Rejected unauthenticated or replayed HDLC frame", error=str(exc))
                    return

        if frame.address == protocol.HDLC_ADDRESS_LOG:
            decoded_log = str(self.detokenizer.detokenize(frame_data))
            logger.info("MCU log", log=decoded_log, raw=frame_data)
            return

        if frame.address == protocol.HDLC_ADDRESS_CONSOLE:
            if self.service:
                await self.service.handle_mcu_frame(
                    protocol.Command.CMD_CONSOLE_WRITE.value, 0, pb.ConsoleWrite(data=frame_data)
                )
            return

        if frame.address == protocol.HDLC_ADDRESS_RPC:
            try:
                pkt = pw_packets.decode(frame_data)
            except (ProtobufDecodeError, ValueError) as exc:
                self.state.serial_decode_errors += 1
                logger.error("Malformed RpcPacket received from MCU", error=str(exc), raw_hex=frame_data.hex())
                return
            spec = protocol.get_operation(pkt)
            cmd_id = spec.command_id if spec and spec.command_id is not None else pkt.method_id
            seq_id = pkt.call_id

            if self.config.serial_shared_secret and self.state.link_cipher is None:
                link_sync = protocol.get_operation(protocol.Command.CMD_LINK_SYNC.value)
                is_link_sync_response = (
                    link_sync is not None
                    and pkt.service_id == link_sync.service_id
                    and pkt.method_id == link_sync.method_id
                    and pkt.type in (PacketType.RESPONSE, PacketType.SERVER_ERROR)
                )
                if not is_link_sync_response:
                    self.state.serial_decode_errors += 1
                    logger.error("Dropping unprotected pre-handshake RPC frame")
                    return

            # Pigweed's native client owns responses to Linux-initiated RPCs, including
            # call-ID correlation, payload decoding, stream events, and completion status.
            # Requests initiated by the MCU are handled below by the application service.
            is_unary_rpc_response = (
                spec is not None
                and getattr(spec.cardinality, "name", "") == "UNARY_UNARY"
                and spec.command_id not in _LEGACY_RAW_REQUESTS
            )
            if not pw_packets.for_server(pkt) and is_unary_rpc_response:
                rpc_status = self.rpc_client.process_packet(frame_data)
                if not rpc_status.ok():
                    logger.warning(
                        "Pigweed RPC client rejected incoming packet",
                        status=str(rpc_status),
                        service_id=pkt.service_id,
                        method_id=pkt.method_id,
                        call_id=pkt.call_id,
                    )
                return

            # pw_rpc: a non-OK status (or SERVER_ERROR) is a failure regardless of payload;
            # successful responses map onto the operation's canonical response id.
            if pkt.type == PacketType.SERVER_ERROR or (pkt.type == PacketType.RESPONSE and pkt.status != 0):
                cmd_id = Status.ERROR.value
            elif pkt.type == PacketType.RESPONSE and spec and spec.expected_responses:
                cmd_id = min(spec.expected_responses)

            payload: ProtobufMessage | bytes = pkt.payload
            if (
                spec
                and spec.response_type
                and (not pw_packets.for_server(pkt) or spec.pb_class is None)
                and pkt.payload
            ):
                try:
                    payload = cast(ProtobufMessage, cast(Any, pw_packets).decode_payload(pkt, spec.response_type))
                except (ProtobufDecodeError, ValueError) as exc:
                    logger.warning(
                        "Unable to parse RPC response payload",
                        service_id=pkt.service_id,
                        method_id=pkt.method_id,
                        error=str(exc),
                    )

            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[MCU -> SERIAL] [CMD:0x%02X] [SVC:0x%08X METH:0x%08X SEQ:%d]",
                    cmd_id,
                    pkt.service_id,
                    pkt.method_id,
                    seq_id,
                )

            if (
                self.state.connection_fsm.negotiating_baud.is_active
                and self.negotiation_future
                and not self.negotiation_future.done()
                and self.negotiation_target_baudrate is not None
                and cmd_id == protocol.Command.CMD_SET_BAUDRATE_RESP.value
            ):
                self.switch_local_baudrate(self.negotiation_target_baudrate)
                self.negotiation_future.set_result(True)
                return

            # SIL-2 Transport Gate: Validate incoming Protobuf payload before dispatch
            if isinstance(payload, ProtobufMessage) and not payload.IsInitialized():
                logger.error("Transport gate rejected uninitialized payload from MCU")
                self.state.serial_decode_errors += 1
                return

            # Correlation and Service dispatch
            self.correlate_frame(cmd_id, payload, seq_id)
            # In pw_rpc, PacketType.RESPONSE is only dispatched to service handlers when the
            # resolved operation is an explicit response/status handler (paired_request_id != None
            # or status code), never re-triggering bidirectional request handlers like MailboxPush.
            resolved_spec = protocol.get_operation(cmd_id)
            is_response_handler = (resolved_spec is not None and resolved_spec.paired_request_id is not None) or (
                cmd_id in SERIAL_FAILURE_STATUS_CODES or cmd_id in SERIAL_SUCCESS_STATUS_CODES
            )
            if self.service and (
                pkt.type != PacketType.RESPONSE
                or (is_response_handler and cmd_id in getattr(self.service, "mcu_registry", {}))
            ):
                dispatch_key = (pkt.service_id, pkt.method_id) if pkt.type == PacketType.REQUEST else cmd_id
                await self.service.handle_mcu_frame(dispatch_key, seq_id, payload)

    def correlate_frame(self, command_id: int, payload: bytes | ProtobufMessage, seq_id: int | None = None) -> None:
        pending = self.current_command
        if logger.is_enabled_for(logging.DEBUG):
            logger.debug(
                "_correlate_frame entry",
                command_id=command_id,
                seq_id=seq_id,
                pending_cmd=(pending.command_id if pending else None),
                pending_seq=(pending.sequence_id if pending else None),
            )
        if pending is None:
            return
        if pending.success is not None:
            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "Pending command already resolved, ignoring further correlation",
                    pending_cmd=pending.command_id,
                    command_id=command_id,
                )
            return
        if pending.sequence_id is not None and seq_id is not None and pending.sequence_id != seq_id:
            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "ignoring_stale_frame_sequence_mismatch",
                    expected_seq=pending.sequence_id,
                    received_seq=seq_id,
                    command_id=command_id,
                )
            return
        spec = get_operation(command_id)
        paired_req_id = spec.paired_request_id if spec else None
        if command_id == pending.command_id or (paired_req_id is not None and paired_req_id == pending.command_id):
            pending.mark_success(payload)
            return
        if command_id in SERIAL_FAILURE_STATUS_CODES:
            pending.mark_failure(command_id)
        elif command_id in SERIAL_SUCCESS_STATUS_CODES and not pending.expected_resp_ids:
            pending.mark_success(payload)

    async def check_baudrate_fallback(self) -> None:
        self.consecutive_crc_errors += 1
        if self.consecutive_crc_errors >= self.config.serial_fallback_threshold:
            logger.error("Fallback to safe baudrate", baud=self.config.serial_safe_baud)
            self.consecutive_crc_errors = 0
            if self.config.serial_baud != self.config.serial_safe_baud:
                await self.negotiate_baudrate(self.config.serial_safe_baud)

    async def send(
        self,
        command_id: int,
        payload: bytes | ProtobufMessage,
        seq_id: int | None = None,
        timeout: float | None = None,
    ) -> bool | bytes | ProtobufMessage:
        """Send an operation, delegating unary RPC lifecycle to Pigweed's native client."""
        if not self.serial or not self.serial.is_open:
            return False

        spec = get_operation(command_id)
        is_link_reset = command_id == protocol.Command.CMD_LINK_RESET.value
        is_handshake = command_id in (protocol.Command.CMD_LINK_SYNC.value, protocol.Command.CMD_LINK_RESET.value)
        if (
            not is_link_reset
            and spec is not None
            and spec.paired_request_id is None
            and spec.service_name
            and spec.method_name
            and getattr(spec.cardinality, "name", "") == "UNARY_UNARY"
        ):
            return await self._send_rpc(spec, payload, timeout)

        is_tracked = not is_handshake and spec is not None and (bool(spec.expected_responses) or spec.requires_ack)

        if not is_tracked:
            return await self.send_raw(command_id, payload, seq_id)

        effective_timeout = timeout if timeout is not None else self._response_timeout

        async with self._flow_lock:
            pending = PendingCommand(
                command_id=command_id,
                expected_resp_ids=list(spec.expected_responses) if spec else [],
            )
            self.current_command = pending
            try:
                retryer = tenacity.AsyncRetrying(
                    stop=tenacity.stop_after_attempt(self.max_attempts),
                    wait=tenacity.wait_exponential(
                        multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE, max=SERIAL_HANDSHAKE_BACKOFF_MAX
                    ),
                    retry=tenacity.retry_if_exception_type(self._RetryableSerialError),
                    reraise=True,
                )

                async def _send_attempt() -> Any:
                    pending.attempts = (pending.attempts or 0) + 1
                    pending.success = None

                    attempt_seq = seq_id
                    if attempt_seq is None:
                        self.tx_sequence_id = (self.tx_sequence_id + 1) & protocol.UINT16_MAX
                        attempt_seq = self.tx_sequence_id
                    pending.sequence_id = attempt_seq

                    if not await self.send_raw(command_id, payload, attempt_seq):
                        raise self._FatalSerialError(None)

                    try:
                        async with asyncio.timeout(effective_timeout):
                            await pending.completion.wait()
                            if pending.success:
                                return pending.response_payload or True
                    except TimeoutError as exc:
                        raise self._RetryableSerialError() from exc

                    if pending.failure_status is not None:
                        raise self._FatalSerialError(pending.failure_status)
                    raise self._RetryableSerialError()

                with structlog.contextvars.bound_contextvars(command_id=command_id):
                    return await retryer(_send_attempt)
            except self._FatalSerialError as exc:
                pending.mark_failure(exc.status)
                return False
            except (self._RetryableSerialError, tenacity.RetryError):
                pending.mark_failure(Status.TIMEOUT.value)
                return False
            finally:
                self.current_command = None

    def _queue_rpc_packet(self, packet: bytes) -> None:
        """Queue a Pigweed-generated RPC packet for encrypted HDLC transmission."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("Pigweed RPC output invoked outside the asyncio event loop") from exc
        task = loop.create_task(self._write_rpc_packet(packet), name="mcubridge-pw-rpc-write")
        self._rpc_write_tasks.add(task)

        def _finish(completed: asyncio.Task[bool]) -> None:
            self._rpc_write_tasks.discard(completed)
            try:
                if not completed.result():
                    logger.error("Pigweed RPC packet could not be written to serial")
            except (asyncio.CancelledError, OSError, RuntimeError, ValueError) as exc:
                logger.error("Pigweed RPC packet write task failed", error=str(exc))

        task.add_done_callback(_finish)

    async def _write_rpc_packet(self, packet: bytes) -> bool:
        """Apply link security and HDLC framing to a packet produced by pw_rpc.Client."""
        if not self.serial or not self.serial.is_open:
            return False
        if not self.state.serial_tx_allowed.is_set():
            try:
                async with asyncio.timeout(FLOW_CONTROL_WAIT_TIMEOUT_SECONDS):
                    await self.state.serial_tx_allowed.wait()
            except TimeoutError:
                logger.warning("Timed out waiting for serial TX flow control before RPC write")
                self.state.mcu_is_paused = False
                self.state.serial_tx_allowed.set()

        cipher = self.state.link_cipher
        if self.config.serial_shared_secret and cipher is None:
            link_sync = protocol.get_operation(protocol.Command.CMD_LINK_SYNC.value)
            try:
                meta = pw_packets.decode(packet)
            except (ProtobufDecodeError, ValueError) as exc:
                logger.error("Refusing malformed pre-authentication Pigweed RPC packet", error=str(exc))
                return False
            if (
                link_sync is None
                or meta.type != PacketType.REQUEST
                or meta.service_id != link_sync.service_id
                or meta.method_id != link_sync.method_id
            ):
                logger.error("Refusing non-handshake Pigweed RPC output before link authentication")
                return False

        from pw_hdlc.encode import ui_frame

        frame_payload = cipher.encrypt(protocol.HDLC_ADDRESS_RPC, packet) if cipher is not None else packet
        encoded = ui_frame(protocol.HDLC_ADDRESS_RPC, frame_payload)
        async with self._serial_write_lock:
            serial_port = self.serial
            if serial_port is None or not serial_port.is_open:
                return False

            def _write_and_flush(ser: serial.Serial, data: bytes) -> None:
                ser.write(data)
                ser.flush()

            try:
                await asyncio.to_thread(_write_and_flush, serial_port, encoded)
            except (AttributeError, OSError, RuntimeError, ValueError) as exc:
                logger.error("Pigweed RPC serial write failed", error=str(exc))
                return False
        self.state.metrics.serial_bytes_sent.inc(len(encoded))
        self.state.metrics.serial_frames_sent.inc()
        return True

    async def _send_rpc(
        self,
        spec: Any,
        payload: bytes | ProtobufMessage,
        timeout: float | None,
    ) -> bool | ProtobufMessage:
        """Invoke one unary method through pw_rpc.Client and await its callbacks."""
        if spec.request_type is None:
            logger.error("Pigweed RPC descriptor has no request type", operation=spec.name)
            return False
        if isinstance(payload, spec.request_type):
            request = payload
        elif isinstance(payload, bytes):
            try:
                request = spec.request_type.FromString(payload)
            except ProtobufDecodeError as exc:
                logger.error("Unable to decode Pigweed RPC request payload", operation=spec.name, error=str(exc))
                return False
        else:
            logger.error("Invalid request type for Pigweed RPC", operation=spec.name)
            return False
        if not request.IsInitialized():
            logger.error("Pigweed RPC request is not initialized", operation=spec.name)
            return False

        method_name = f"{spec.service_name}/{spec.method_name}"
        try:
            rpc_method = self.rpc_client.channel(protocol.HDLC_ADDRESS_RPC).method(method_name)
        except (KeyError, ValueError) as exc:
            logger.error("Pigweed RPC method is not present in generated descriptors", method=method_name, error=str(exc))
            return False

        effective_timeout = timeout if timeout is not None else self._response_timeout
        for attempt in range(self.max_attempts):
            loop = asyncio.get_running_loop()
            completion: asyncio.Future[bool | ProtobufMessage] = loop.create_future()
            response: ProtobufMessage | None = None

            def _on_next(_call: Any, message: ProtobufMessage) -> None:
                nonlocal response
                response = message

            def _on_completed(_call: Any, status: Any) -> None:
                if completion.done():
                    return
                if status.ok():
                    completion.set_result(response if response is not None else True)
                else:
                    completion.set_result(False)

            def _on_error(_call: Any, error: Any) -> None:
                if not completion.done():
                    logger.warning("Pigweed unary RPC failed", method=method_name, error=str(error))
                    completion.set_result(False)

            call: Any = None
            try:
                call = rpc_method.invoke(
                    request,
                    on_next=_on_next,
                    on_completed=_on_completed,
                    on_error=_on_error,
                )
                self._active_rpc_calls.append(call)
                async with asyncio.timeout(effective_timeout):
                    return await completion
            except TimeoutError:
                if call is not None:
                    try:
                        call.cancel()
                    except (KeyError, RuntimeError, ValueError) as exc:
                        logger.debug("Pigweed RPC cancellation raced with completion", method=method_name, error=str(exc))
                if attempt + 1 >= self.max_attempts:
                    logger.warning("Pigweed unary RPC timed out", method=method_name, attempts=self.max_attempts)
                    return False
                await asyncio.sleep(min(SERIAL_HANDSHAKE_BACKOFF_BASE * (2**attempt), SERIAL_HANDSHAKE_BACKOFF_MAX))
            except asyncio.CancelledError:
                if call is not None:
                    try:
                        call.cancel()
                    except (KeyError, RuntimeError, ValueError) as exc:
                        logger.debug("Pigweed RPC cancellation raced with task cancellation", method=method_name, error=str(exc))
                raise
            except Exception as exc:
                logger.error("Unable to invoke Pigweed unary RPC", method=method_name, error=str(exc))
                return False
            finally:
                if call is not None and call in self._active_rpc_calls:
                    self._active_rpc_calls.remove(call)
        return False

    async def send_raw(self, command_id: int, payload: bytes | ProtobufMessage, seq_id: int | None = None) -> bool:
        """Low-level send logic without tracking."""
        if not self.serial:
            return False

        is_baudrate_negotiation = (
            command_id == protocol.Command.CMD_SET_BAUDRATE.value
            and self.state.connection_fsm.negotiating_baud.is_active
        )
        if not self.state.serial_tx_allowed.is_set() and not is_baudrate_negotiation:
            try:
                async with asyncio.timeout(FLOW_CONTROL_WAIT_TIMEOUT_SECONDS):
                    await self.state.serial_tx_allowed.wait()
            except TimeoutError:
                logger.warning(
                    "Timed out waiting for serial TX flow control, auto-resuming TX",
                    timeout=FLOW_CONTROL_WAIT_TIMEOUT_SECONDS,
                )
                self.state.mcu_is_paused = False
                self.state.serial_tx_allowed.set()

        if seq_id is None:
            self.tx_sequence_id = (self.tx_sequence_id + 1) & protocol.UINT16_MAX
            seq_id = self.tx_sequence_id

        async with self._serial_write_lock:
            serial_port = self.serial
            if not serial_port:
                return False

            cipher = self.state.link_cipher
            is_link_reset = command_id == protocol.Command.CMD_LINK_RESET.value
            is_link_sync = command_id == protocol.Command.CMD_LINK_SYNC.value

            if command_id == protocol.Command.CMD_CONSOLE_WRITE.value:
                if isinstance(payload, bytes):
                    data = payload
                elif hasattr(payload, "data"):
                    data = cast(bytes, getattr(payload, "data", b""))
                else:
                    data = b""
                if self.config.serial_shared_secret and cipher is None:
                    return False
                if cipher is None:
                    encoded = protocol.encode_hdlc_console(data)
                else:
                    from pw_hdlc.encode import ui_frame

                    envelope = cipher.encrypt(protocol.HDLC_ADDRESS_CONSOLE, data)
                    encoded = ui_frame(protocol.HDLC_ADDRESS_CONSOLE, envelope)
            else:
                if self.config.serial_shared_secret:
                    if cipher is None and not (is_link_reset or is_link_sync):
                        return False
                    if cipher is not None and is_link_sync:
                        return False
                if cipher is not None and not is_link_reset:
                    from pw_hdlc.encode import ui_frame

                    packet = protocol.encode_rpc_packet(command_id, payload, call_id=seq_id or 1)
                    envelope = cipher.encrypt(protocol.HDLC_ADDRESS_RPC, packet)
                    encoded = ui_frame(protocol.HDLC_ADDRESS_RPC, envelope)
                else:
                    encoded = protocol.encode_hdlc_rpc(command_id, payload, call_id=seq_id or 1)

            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[SERIAL -> MCU] [CMD:0x%02X] [HDLC]: [%s]",
                    command_id,
                    encoded.hex().upper(),
                )

            try:

                def _write_and_flush(ser: serial.Serial, data: bytes) -> None:
                    ser.write(data)
                    ser.flush()

                await asyncio.to_thread(_write_and_flush, serial_port, encoded)
                self.state.metrics.serial_bytes_sent.inc(len(encoded))
                self.state.metrics.serial_frames_sent.inc()
                return True
            except (AttributeError, OSError, RuntimeError, ValueError) as exc:
                logger.error("Serial write failed", error=str(exc))
                return False

    async def negotiate_baudrate(self, target_baud: int) -> bool:
        payload = pb.SetBaudratePacket(baudrate=target_baud)
        was_synchronized = self.state.is_synchronized
        self.state.connection_fsm.start_negotiating()
        self.negotiation_target_baudrate = target_baud
        try:
            self.negotiation_future = asyncio.get_running_loop().create_future()
            if not await self.send_raw(protocol.Command.CMD_SET_BAUDRATE.value, payload):
                return False
            async with asyncio.timeout(SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT):
                await self.negotiation_future
            await asyncio.sleep(protocol.BAUDRATE_CHANGE_DELAY_MS / 1000)
            if was_synchronized:
                self.state.connection_fsm.synchronize()
            return True
        except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
            logger.error("Baudrate negotiation failed", error=str(exc))
            return False
        finally:
            self.negotiation_future = None
            self.negotiation_target_baudrate = None
            with anyio.CancelScope(shield=True):
                if self.state.connection_fsm.negotiating_baud.is_active:
                    self.state.connection_fsm.connect()
