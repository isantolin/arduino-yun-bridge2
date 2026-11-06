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
from pw_rpc import packets as pw_packets
from pw_rpc.internal.packet_pb2 import PacketType, RpcPacket
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
    OperationSpec,
    Status,
    get_operation,
)
from mcubridge.protocol.structures import PendingCommand
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.serial")

_SYSTEM_SERVICE: Final[Any] = protocol.PW_SERVICES["rpc.pb.SystemService"]
_LINK_SYNC_RPC: Final[tuple[int, int]] = (
    int(_SYSTEM_SERVICE.id),
    int(_SYSTEM_SERVICE.methods["LinkSync"].id),
)
_LINK_RESET_RPC: Final[tuple[int, int]] = (
    int(_SYSTEM_SERVICE.id),
    int(_SYSTEM_SERVICE.methods["LinkReset"].id),
)
_SET_BAUDRATE_RPC: Final[tuple[int, int]] = (
    int(_SYSTEM_SERVICE.id),
    int(_SYSTEM_SERVICE.methods["SetBaudrate"].id),
)
_HANDSHAKE_RPCS: Final[frozenset[tuple[int, int]]] = frozenset({
    _LINK_SYNC_RPC,
    _LINK_RESET_RPC,
})


def _resolve_rpc_target(
    target: int | tuple[int, int] | RpcPacket,
) -> tuple[OperationSpec | None, tuple[int, int] | None]:
    """Resolve an operation spec and canonical (service_id, method_id) tuple."""
    if isinstance(target, RpcPacket):
        rpc_key = (target.service_id, target.method_id)
        return protocol.get_operation(target), rpc_key
    if isinstance(target, tuple):
        return protocol.get_operation(target), target
    spec = get_operation(target)
    if spec is not None and spec.service_id is not None and spec.method_id is not None:
        return spec, (spec.service_id, spec.method_id)
    if spec is not None and spec.paired_request_id is not None:
        req_spec = get_operation(spec.paired_request_id)
        if req_spec is not None and req_spec.service_id is not None and req_spec.method_id is not None:
            return spec, (req_spec.service_id, req_spec.method_id)
    return spec, None

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
        self.current_rpc_call: tuple[int, int, int] | None = None
        self._flow_lock = asyncio.Lock()
        self._serial_write_lock = asyncio.Lock()

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
            self.current_rpc_call = None

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

            rpc_key = (pkt.service_id, pkt.method_id)
            spec = protocol.get_operation(pkt)
            seq_id = pkt.call_id

            if self.config.serial_shared_secret and self.state.link_cipher is None:
                is_link_sync_response = (
                    rpc_key == _LINK_SYNC_RPC
                    and pkt.type in (PacketType.RESPONSE, PacketType.SERVER_ERROR)
                )
                if not is_link_sync_response:
                    self.state.serial_decode_errors += 1
                    logger.error("Dropping unprotected pre-handshake RPC frame")
                    return

            is_error_packet = pkt.type == PacketType.SERVER_ERROR or (
                pkt.type == PacketType.RESPONSE and pkt.status != 0
            )

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
                    "[MCU -> SERIAL] [TYPE:%d STATUS:%d] [SVC:0x%08X METH:0x%08X SEQ:%d]",
                    int(pkt.type),
                    pkt.status,
                    pkt.service_id,
                    pkt.method_id,
                    seq_id,
                )

            if (
                self.state.connection_fsm.negotiating_baud.is_active
                and self.negotiation_future
                and not self.negotiation_future.done()
                and self.negotiation_target_baudrate is not None
                and rpc_key == _SET_BAUDRATE_RPC
                and pkt.type == PacketType.RESPONSE
                and pkt.status == 0
            ):
                self.switch_local_baudrate(self.negotiation_target_baudrate)
                self.negotiation_future.set_result(True)
                return

            # SIL-2 Transport Gate: Validate incoming Protobuf payload before dispatch
            if isinstance(payload, ProtobufMessage) and not payload.IsInitialized():
                logger.error("Transport gate rejected uninitialized payload from MCU")
                self.state.serial_decode_errors += 1
                return

            await self._correlate_and_dispatch_rpc(pkt, spec, rpc_key, seq_id, payload, is_error_packet)

    async def _correlate_and_dispatch_rpc(
        self,
        pkt: RpcPacket,
        spec: OperationSpec | None,
        rpc_key: tuple[int, int],
        seq_id: int,
        payload: bytes | ProtobufMessage,
        is_error_packet: bool,
    ) -> None:
        is_raw_status = (
            spec is None
            and pkt.service_id == pkt.method_id
            and (pkt.method_id in SERIAL_SUCCESS_STATUS_CODES or pkt.method_id in SERIAL_FAILURE_STATUS_CODES)
        )
        if pkt.type in (PacketType.RESPONSE, PacketType.SERVER_ERROR) or is_raw_status:
            self.correlate_frame(pkt, payload, seq_id)

        if not self.service:
            return

        if is_error_packet:
            await self.service.handle_mcu_frame(Status.ERROR.value, seq_id, payload)
        elif is_raw_status:
            if pkt.method_id in getattr(self.service, "mcu_registry", {}):
                await self.service.handle_mcu_frame(pkt.method_id, seq_id, payload)
        elif pw_packets.for_server(pkt) or pkt.type == PacketType.SERVER_STREAM:
            await self.service.handle_mcu_frame(rpc_key, seq_id, payload)
        elif pkt.type == PacketType.RESPONSE:
            resp_spec = protocol.BY_RPC_RESPONSE.get(rpc_key)
            if (
                resp_spec is not None
                and resp_spec.command_id is not None
                and resp_spec.command_id in getattr(self.service, "mcu_registry", {})
            ):
                await self.service.handle_mcu_frame(resp_spec.command_id, seq_id, payload)

    def _unpack_correlation_frame(
        self,
        frame_or_key: RpcPacket | tuple[int, int] | int,
        seq_id: int | None,
        status: int,
        packet_type: int,
    ) -> tuple[tuple[int, int] | None, int | None, int, int, int | None]:
        if isinstance(frame_or_key, RpcPacket):
            key: tuple[int, int] = (frame_or_key.service_id, frame_or_key.method_id)
            raw_status = (
                frame_or_key.method_id
                if frame_or_key.service_id == frame_or_key.method_id and key not in protocol.BY_RPC
                else None
            )
            return key, frame_or_key.call_id, frame_or_key.status, int(frame_or_key.type), raw_status
        if isinstance(frame_or_key, tuple):
            return frame_or_key, seq_id, status, packet_type, None
        _, resolved_key = _resolve_rpc_target(frame_or_key)
        return resolved_key, seq_id, status, packet_type, frame_or_key

    def correlate_frame(
        self,
        frame_or_key: RpcPacket | tuple[int, int] | int,
        payload: bytes | ProtobufMessage = b"",
        seq_id: int | None = None,
        *,
        status: int = 0,
        packet_type: int = int(PacketType.RESPONSE),
    ) -> None:
        pending = self.current_command
        rpc_key, actual_call_id, actual_status, actual_type, raw_status = self._unpack_correlation_frame(
            frame_or_key, seq_id, status, packet_type
        )
        expected_call = self.current_rpc_call
        if expected_call is None and pending is not None:
            _, pending_rpc = _resolve_rpc_target(pending.command_id)
            if pending_rpc is not None:
                expected_call = (pending_rpc[0], pending_rpc[1], pending.sequence_id or 0)

        if logger.is_enabled_for(logging.DEBUG):
            logger.debug(
                "_correlate_frame entry",
                rpc_key=rpc_key,
                seq_id=actual_call_id,
                pending_call=expected_call,
                pending_seq=(pending.sequence_id if pending else None),
            )
        if pending is None:
            return
        if pending.success is not None:
            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "Pending command already resolved, ignoring further correlation",
                    pending_cmd=pending.command_id,
                    rpc_key=rpc_key,
                )
            return
        if pending.sequence_id is not None and actual_call_id is not None and pending.sequence_id != actual_call_id:
            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "ignoring_stale_frame_sequence_mismatch",
                    expected_seq=pending.sequence_id,
                    received_seq=actual_call_id,
                    rpc_key=rpc_key,
                )
            return

        if raw_status is not None:
            if raw_status in SERIAL_FAILURE_STATUS_CODES:
                pending.mark_failure(raw_status)
                return
            if raw_status in SERIAL_SUCCESS_STATUS_CODES and not pending.expected_resp_ids:
                pending.mark_success(payload)
                return

        if actual_type == int(PacketType.SERVER_ERROR) or (
            actual_type == int(PacketType.RESPONSE) and actual_status != 0
        ):
            if expected_call is None or rpc_key is None or rpc_key == (expected_call[0], expected_call[1]):
                pending.mark_failure(Status.ERROR.value)
            return

        if (
            actual_type == int(PacketType.RESPONSE)
            and rpc_key is not None
            and expected_call is not None
            and rpc_key == (expected_call[0], expected_call[1])
        ):
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
        command_id: int | tuple[int, int],
        payload: bytes | ProtobufMessage = b"",
        seq_id: int | None = None,
        timeout: float | None = None,
    ) -> bool | bytes | ProtobufMessage:
        """Unified send method with automatic tracking, retries, and optional response return. [FLATTENED]"""
        if not self.serial or not self.serial.is_open:
            return False

        spec, rpc_key = _resolve_rpc_target(command_id)
        is_response_packet = spec is not None and spec.paired_request_id is not None
        is_tracked = (
            rpc_key is not None
            and not is_response_packet
            and rpc_key not in _HANDSHAKE_RPCS
            and (spec is None or bool(spec.expected_responses) or spec.requires_ack)
        )

        if not is_tracked or rpc_key is None:
            return await self.send_raw(command_id, payload, seq_id)

        effective_timeout = timeout if timeout is not None else self._response_timeout
        cmd_val = (
            spec.command_id
            if spec is not None and spec.command_id is not None
            else (command_id if isinstance(command_id, int) else rpc_key[1])
        )

        async with self._flow_lock:
            pending = PendingCommand(
                command_id=cmd_val,
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
                    self.current_rpc_call = (rpc_key[0], rpc_key[1], attempt_seq)

                    if not await self.send_raw(rpc_key, payload, attempt_seq):
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

                with structlog.contextvars.bound_contextvars(
                    command_id=cmd_val,
                    service_id=rpc_key[0],
                    method_id=rpc_key[1],
                ):
                    return await retryer(_send_attempt)
            except self._FatalSerialError as exc:
                pending.mark_failure(exc.status)
                return False
            except (self._RetryableSerialError, tenacity.RetryError):
                pending.mark_failure(Status.TIMEOUT.value)
                return False
            finally:
                self.current_command = None
                self.current_rpc_call = None

    async def send_raw(
        self,
        command_id: int | tuple[int, int],
        payload: bytes | ProtobufMessage,
        seq_id: int | None = None,
        *,
        packet_type: int | None = None,
        status: int = 0,
    ) -> bool:
        """Low-level send logic without tracking."""
        if not self.serial:
            return False

        spec, rpc_key = _resolve_rpc_target(command_id)
        is_baudrate_negotiation = (
            rpc_key == _SET_BAUDRATE_RPC
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
        call_id = seq_id or 1

        async with self._serial_write_lock:
            serial_port = self.serial
            if not serial_port:
                return False

            cipher = self.state.link_cipher
            is_link_reset = rpc_key == _LINK_RESET_RPC
            is_link_sync = rpc_key == _LINK_SYNC_RPC
            is_console = (
                (spec is not None and spec.command_id == protocol.Command.CMD_CONSOLE_WRITE.value)
                or command_id == protocol.Command.CMD_CONSOLE_WRITE.value
            )

            if is_console:
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

                from pw_hdlc.encode import ui_frame

                target_for_encode: int | tuple[int, int] = rpc_key if rpc_key is not None else command_id
                eff_type = (
                    packet_type
                    if packet_type is not None
                    else (
                        int(PacketType.RESPONSE)
                        if (spec is not None and spec.paired_request_id is not None)
                        else int(PacketType.REQUEST)
                    )
                )
                if cipher is not None and not is_link_reset:
                    packet = protocol.encode_rpc_packet(
                        target_for_encode,
                        payload,
                        call_id=call_id,
                        packet_type=eff_type,
                        status=status,
                    )
                    envelope = cipher.encrypt(protocol.HDLC_ADDRESS_RPC, packet)
                    encoded = ui_frame(protocol.HDLC_ADDRESS_RPC, envelope)
                else:
                    encoded = protocol.encode_hdlc_rpc(
                        target_for_encode,
                        payload,
                        call_id=call_id,
                        packet_type=eff_type,
                        status=status,
                    )

            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[SERIAL -> MCU] [RPC:%s] [HDLC]: [%s]",
                    rpc_key if rpc_key is not None else f"0x{command_id:02X}",
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
