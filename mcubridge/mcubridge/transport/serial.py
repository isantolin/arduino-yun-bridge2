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
from collections import deque
import logging
import os
import socket
from pathlib import Path
import time
from collections.abc import Callable, Coroutine
from typing import Any, Final, Protocol, cast

import anyio
from pw_transfer.client import Error as PwTransferError, Manager as PwTransferManager
import serial
import structlog
import tenacity
from google.protobuf.message import DecodeError as ProtobufDecodeError, Message as ProtobufMessage
from pw_hdlc.decode import Frame, FrameDecoder, FrameStatus
from pw_hdlc.encode import ui_frame
from pw_rpc import ids as pw_ids, packets as pw_packets
from pw_rpc.callback_client.call import UnaryCall
from pw_rpc.callback_client.impl import Impl as PwCallbackClientImpl
from pw_rpc.client import Client as PwRpcClient
from pw_rpc.descriptors import Channel as PwRpcChannel, Method as PwRpcMethod, RpcIds
from pw_rpc.internal.packet_pb2 import PacketType, RpcPacket
from pw_tokenizer import detokenize, elf_reader
from pw_tokenizer.tokens import DatabaseFormatError

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcu_wire_pb2 as pb
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
from mcubridge.services.handshake import SerialHandshakeFatal
from mcubridge.state.context import RuntimeState

logger = structlog.get_logger("mcubridge.serial")

_LINK_SYNC_SPEC: Final = protocol.BY_NAME["rpc.pb.SystemService/LinkSync"]
# Upstream pw_transfer is not a project operation: its calls are opened by the
# pw_transfer Manager on this client, so every client-side packet of the
# service belongs to the Pigweed client (transfer_read / transfer_write).
_PW_TRANSFER_SERVICE_ID: Final[int] = pw_ids.calculate("pw.transfer.Transfer")
_LINK_RESET_SPEC: Final = protocol.BY_NAME["rpc.pb.SystemService/LinkReset"]
# Console bytes travel on HDLC_ADDRESS_CONSOLE, not as pw_rpc packets to the MCU.
_CONSOLE_WRITE_SPEC: Final = protocol.BY_NAME["rpc.pb.ConsoleService/Write"]
_HANDSHAKE_RAW_NAMES: Final[frozenset[str]] = frozenset(
    {
        "rpc.pb.SystemService/LinkSync",
        "rpc.pb.SystemService/LinkReset",
    }
)
_SERIAL_STATUS_CODES: Final[frozenset[int]] = SERIAL_FAILURE_STATUS_CODES | SERIAL_SUCCESS_STATUS_CODES
_STREAM_EVENT_QUEUE_MAX: Final[int] = 64
_TRANSFER_CHUNK_SIZE_BYTES: Final[int] = 128

DEFAULT_TOKEN_DATABASE_CANDIDATES: Final[tuple[Path, ...]] = (
    Path("/etc/mcubridge/tokens.csv"),
    Path("/etc/mcubridge/tokens.bin"),
    Path("/usr/share/mcubridge/tokens.csv"),
)


class SerialBridgeService(Protocol):
    """Protocol for bridge service callbacks invoked by SerialTransport."""

    async def on_serial_connected(self) -> None: ...

    async def on_serial_disconnected(self) -> None: ...

    async def handle_mcu_frame(
        self, operation_key: int | str | tuple[int, int], seq_id: int, payload: bytes | ProtobufMessage, /
    ) -> Any: ...


def resolve_serial_url(port_str: str) -> str:
    """Resolve serial device or network endpoint string into a canonical serial URL. [SIL-2]"""
    if not port_str:
        return ""
    clean = port_str.strip()
    if clean.startswith(("tcp://", "wifi://", "socket://")):
        _, _, host_port = clean.partition("://")
        return f"socket://{host_port}" if ":" in host_port else f"socket://{host_port}:9000"
    if ":" in clean and not clean.startswith(("/", ".")):
        return f"socket://{clean}"
    return clean


def decode_tokenized_log_payload(
    frame_data: bytes,
    detokenizer: detokenize.Detokenizer,
) -> str:
    """Detokenize a Pigweed log payload or preserve printable UTF-8 diagnostic text."""
    detokenized_msg = detokenizer.detokenize(frame_data)
    if detokenized_msg.ok():
        return str(detokenized_msg)
    try:
        candidate = frame_data.decode("utf-8")
    except UnicodeDecodeError:
        return str(detokenized_msg)
    stripped = candidate.rstrip("\r\n")
    if stripped and all(ch.isprintable() or ch in "\r\n\t" for ch in stripped):
        return stripped
    return str(detokenized_msg)


def cancel_rpc_call(call: Any, reason: str, **log_context: Any) -> bool:
    """Cancel a Pigweed RPC call, absorbing benign completion races."""
    if call is None:
        return False
    try:
        call.cancel()
        return True
    except (AttributeError, KeyError, OSError, RuntimeError, ValueError) as exc:
        logger.debug(reason, error=str(exc), **log_context)
        return False


def _write_and_flush(ser: serial.Serial, data: bytes) -> None:
    """Thread target to write raw bytes to serial device and flush immediately. [SIL-2]"""
    ser.write(data)
    ser.flush()


def close_serial_port(ser: serial.Serial) -> None:
    """Thread target to close the port and release its raw socket deterministically. [SIL-2]

    pyserial's socket:// handler drops its socket reference when shutdown() fails
    (peer already gone) without calling close(), leaving the descriptor to the GC and
    raising ResourceWarning. Closing the raw socket ourselves is idempotent and keeps
    teardown deterministic for tcp:// and wifi:// links.
    """
    raw_socket = getattr(ser, "_socket", None)
    if isinstance(raw_socket, socket.socket):
        try:
            raw_socket.shutdown(socket.SHUT_RDWR)
        except OSError as exc:
            logger.debug("Raw socket shutdown handled during close", error=str(exc))
    try:
        ser.close()
    finally:
        if isinstance(raw_socket, socket.socket):
            try:
                raw_socket.close()
            except OSError as exc:
                logger.debug("Raw socket close handled during teardown", error=str(exc))


class SerialTransport:
    """High-performance asyncio serial/wireless transport with flattened pipeline. [SIL-2]"""

    config: RuntimeConfig
    state: RuntimeState
    service: SerialBridgeService | None
    serial: serial.Serial | None
    stop_event: asyncio.Event
    rpc_client: PwRpcClient

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        service: SerialBridgeService | None,
        rpc_packet_writer: Callable[[bytes], Coroutine[Any, Any, bool]] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.service: SerialBridgeService | None = service
        self._rpc_packet_writer = rpc_packet_writer or self._write_rpc_packet
        self.serial: serial.Serial | None = None

        self.stop_event = asyncio.Event()
        self.consecutive_crc_errors = 0
        self.tx_sequence_id = 0

        self._serial_write_lock = asyncio.Lock()
        try:
            self._event_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None
        self._rpc_write_tasks: set[asyncio.Task[bool]] = set()
        self._active_rpc_calls: set[Any] = set()
        # Directional routing only; pw_rpc.Client owns call lifecycle/correlation.
        self._active_client_route_keys: set[tuple[int, int, int, int]] = set()
        self._stream_call: Any | None = None
        self._stream_event_queue: deque[ProtobufMessage] = deque(maxlen=_STREAM_EVENT_QUEUE_MAX)
        self._stream_backpressure_drops: int = 0
        self._transfer_manager: PwTransferManager | None = None
        self._transfer_lock = asyncio.Lock()
        self.rpc_client = PwRpcClient(
            PwCallbackClientImpl(),
            [PwRpcChannel(protocol.HDLC_ADDRESS_RPC, self.queue_rpc_packet)],
            protocol.PW_SERVICES,
        )

        self._ack_timeout = max(self.config.serial_retry_timeout or 0, float(protocol.DEFAULT_ACK_TIMEOUT_MS) / 1000.0)
        self._response_timeout = max(self.config.serial_response_timeout or 0, self._ack_timeout)
        self.max_attempts = max(1, self.config.serial_retry_attempts or 1)
        self.decoder = FrameDecoder()
        self.detokenizer = self.init_detokenizer()

    @staticmethod
    def init_detokenizer() -> detokenize.Detokenizer:
        """Initialize Pigweed detokenizer, optionally loading a token database from disk. [SIL-2]"""
        candidate_paths: list[Path] = []
        env_path = os.environ.get("MCUBRIDGE_TOKEN_DATABASE")
        if env_path:
            candidate_paths.append(Path(env_path))
        candidate_paths.extend(DEFAULT_TOKEN_DATABASE_CANDIDATES)
        for path in candidate_paths:
            if path.is_file():
                try:
                    dt = detokenize.Detokenizer(path)
                    logger.info("Loaded Pigweed tokenizer database", path=str(path))
                    return dt
                except (OSError, ValueError, TypeError, DatabaseFormatError, elf_reader.FileDecodeError) as exc:
                    logger.warning("Failed to load Pigweed tokenizer database", path=str(path), error=str(exc))
        return detokenize.Detokenizer()

    def decode_log_payload(self, frame_data: bytes) -> str:
        """Detokenize a Pigweed log payload or preserve printable UTF-8 diagnostic text."""
        return decode_tokenized_log_payload(frame_data, self.detokenizer)

    def switch_local_baudrate(self, target_baud: int) -> None:
        try:
            if self.serial:
                self.serial.baudrate = target_baud
                logger.info("Local UART switched baudrate", baud=target_baud)
        except (AttributeError, OSError, ValueError) as e:
            raise RuntimeError(f"UART access failed: {e}") from e

    async def reset(self) -> None:
        for call in tuple(self._active_rpc_calls):
            cancel_rpc_call(call, "Unable to cancel pending Pigweed RPC during reset")
        self.cancel_pin_stream()
        self._active_rpc_calls.clear()
        self._active_client_route_keys.clear()
        self._stream_event_queue.clear()

    def cancel_pin_stream(self) -> bool:
        """Explicitly cancel any active server-streaming RPC call."""
        call = self._stream_call
        self._stream_call = None
        return cancel_rpc_call(call, "Unable to cancel Pigweed server-streaming RPC")

    def _enqueue_stream_event(self, message: ProtobufMessage) -> None:
        """Buffer an incoming Pigweed server-streaming message with bounded backpressure."""
        if not message.IsInitialized():
            logger.error("Transport gate rejected uninitialized stream payload from MCU")
            self.state.serial_decode_errors += 1
            return
        if len(self._stream_event_queue) == _STREAM_EVENT_QUEUE_MAX:
            self._stream_backpressure_drops += 1
            logger.warning(
                "Dropped oldest Pigweed stream event due to backpressure",
                drops=self._stream_backpressure_drops,
            )
        self._stream_event_queue.append(message)

    async def _drain_stream_events(self) -> None:
        """Dispatch buffered Pigweed server-streaming events to the application service."""
        gpio = getattr(self.service, "gpio", None) if self.service is not None else None
        while self._stream_event_queue:
            event = self._stream_event_queue.popleft()
            if gpio is not None and isinstance(event, pb.PinUpdateEvent):
                await gpio.handle_pin_update_event(event)

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
        self._event_loop = asyncio.get_running_loop()
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
                await self._close_serial_handle(self.serial, "cleanup")
                self.serial = None

    async def _close_serial_handle(self, serial_port: serial.Serial, context: str) -> None:
        """Cancel pending pyserial I/O and close the port on a worker thread."""
        try:
            serial_port.cancel_read()
            serial_port.cancel_write()
        except (AttributeError, OSError) as exc:
            logger.debug("Failed to cancel pending serial I/O", context=context, error=str(exc))
        try:
            await asyncio.to_thread(close_serial_port, serial_port)
        except (AttributeError, OSError) as exc:
            logger.error("Failed to close serial port", context=context, error=str(exc))

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
            await self._close_serial_handle(self.serial, "stop")

    async def feed_data(self, data: bytes | memoryview) -> None:
        """Feed incoming serial bytes into the HDLC frame decoder and dispatch valid frames."""
        raw_bytes = bytes(data) if isinstance(data, memoryview) else data
        for frame in self.decoder.process(raw_bytes):
            if not frame.ok():
                self.state.serial_decode_errors += 1
                if frame.status == FrameStatus.FCS_MISMATCH:
                    self.state.metrics.serial_crc_errors += 1
                logger.debug(
                    "Dropping corrupt HDLC frame",
                    status=str(frame.status),
                    length=len(frame.raw_decoded),
                    decode_errors=self.state.serial_decode_errors,
                )
                await self.check_baudrate_fallback()
                continue
            self.consecutive_crc_errors = 0
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

    @staticmethod
    def _rpc_packet_key(pkt: RpcPacket) -> tuple[int, int, int, int]:
        return (pkt.channel_id, pkt.service_id, pkt.method_id, pkt.call_id)

    async def _route_to_pigweed_client(
        self, pkt: RpcPacket, spec: protocol.OperationSpec | None, frame_data: bytes
    ) -> bool:
        """Route responses for Linux-initiated calls to the upstream Pigweed client.

        Client.process_packet() returns OK even when a response is not pending.
        This set distinguishes response direction only; Pigweed still owns call
        state, completion, cancellation, and timeout handling.
        """
        if pw_packets.for_server(pkt):
            return False
        if pkt.service_id == _PW_TRANSFER_SERVICE_ID:
            status = self.rpc_client.process_packet(frame_data)
            logger.debug(
                "Routed MCU pw_transfer packet",
                packet_type=pkt.type,
                call_id=pkt.call_id,
                length=len(pkt.payload),
                status=str(status),
            )
            return True

        packet_key = self._rpc_packet_key(pkt)
        is_client_response = (
            spec is not None
            and spec.name not in _HANDSHAKE_RAW_NAMES
            and pkt.type in (PacketType.RESPONSE, PacketType.SERVER_ERROR, PacketType.SERVER_STREAM)
            and packet_key in self._active_client_route_keys
        )
        if not is_client_response:
            return False

        if pkt.type != PacketType.SERVER_STREAM:
            self._active_client_route_keys.discard(packet_key)
        self.rpc_client.process_packet(frame_data)
        await self._drain_stream_events()
        return True

    async def _process_rpc_packet(self, frame_data: bytes) -> None:
        """Decode, authorize, and dispatch an incoming HDLC_ADDRESS_RPC payload."""
        try:
            pkt = pw_packets.decode(frame_data)
        except (ProtobufDecodeError, ValueError) as exc:
            self.state.serial_decode_errors += 1
            logger.error("Malformed RpcPacket received from MCU", error=str(exc), raw_hex=frame_data.hex())
            return
        spec = protocol.get_operation(pkt)
        seq_id = pkt.call_id

        if self.config.serial_shared_secret and self.state.link_cipher is None:
            is_link_sync_response = spec is _LINK_SYNC_SPEC and pkt.type in (
                PacketType.RESPONSE,
                PacketType.SERVER_ERROR,
            )
            if not is_link_sync_response:
                self.state.serial_decode_errors += 1
                logger.error("Dropping unprotected pre-handshake RPC frame")
                return

        if await self._route_to_pigweed_client(pkt, spec, frame_data):
            return

        # pw_rpc: a non-OK status (or SERVER_ERROR) is a failure regardless of payload;
        # successful responses map onto the operation's canonical service/method tuple.
        is_error_pkt = pkt.type == PacketType.SERVER_ERROR or (
            pkt.type == PacketType.RESPONSE and pkt.status != Status.OK.value
        )
        dispatch_key: int | str | tuple[int, int]
        if is_error_pkt:
            dispatch_key = pkt.status if pkt.status != Status.OK.value else Status.INTERNAL.value
        elif spec is None and pkt.method_id in _SERIAL_STATUS_CODES:
            dispatch_key = pkt.method_id
        else:
            dispatch_key = (pkt.service_id, pkt.method_id)

        payload: ProtobufMessage | bytes = pkt.payload
        if spec and spec.response_type and (not pw_packets.for_server(pkt) or spec.pb_class is None) and pkt.payload:
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
                "[MCU -> SERIAL] [SVC:0x%08X METH:0x%08X SEQ:%d]",
                pkt.service_id,
                pkt.method_id,
                seq_id,
            )

        # SIL-2 Transport Gate: Validate incoming Protobuf payload before dispatch
        if isinstance(payload, ProtobufMessage) and not payload.IsInitialized():
            logger.error("Transport gate rejected uninitialized payload from MCU")
            self.state.serial_decode_errors += 1
            return

        # In pw_rpc, PacketType.RESPONSE is only dispatched to service handlers when the
        # resolved operation is an explicit raw handshake or status handler, never
        # re-triggering bidirectional request handlers like MailboxPush.
        is_response_handler = (
            spec is not None and spec.name in _HANDSHAKE_RAW_NAMES
        ) or dispatch_key in _SERIAL_STATUS_CODES
        if self.service and (
            pkt.type != PacketType.RESPONSE
            or (is_response_handler and dispatch_key in getattr(self.service, "mcu_registry", {}))
        ):
            await self.service.handle_mcu_frame(dispatch_key, seq_id, payload)

    async def process_hdlc_frame(self, frame: Frame) -> None:
        """Processes a validated HDLC frame received from the serial link. [SIL-2]"""
        self.state.metrics.serial_bytes_received += len(frame.raw_encoded)
        self.state.metrics.serial_frames_received += 1
        frame_data = frame.data

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
            logger.info("MCU log", log=self.decode_log_payload(frame_data), raw=frame_data)
            return

        if frame.address == protocol.HDLC_ADDRESS_CONSOLE:
            if self.service:
                await self.service.handle_mcu_frame(_CONSOLE_WRITE_SPEC.name, 0, pb.ConsoleWrite(data=frame_data))
            return

        if frame.address == protocol.HDLC_ADDRESS_RPC:
            await self._process_rpc_packet(frame_data)

    async def check_baudrate_fallback(self) -> None:
        self.consecutive_crc_errors += 1
        if self.consecutive_crc_errors >= self.config.serial_fallback_threshold:
            logger.error("Fallback to safe baudrate", baud=self.config.serial_safe_baud)
            self.consecutive_crc_errors = 0
            if self.config.serial_baud != self.config.serial_safe_baud:
                await self.negotiate_baudrate(self.config.serial_safe_baud)

    @property
    def rpc_channel(self) -> Any:
        """Return Pigweed's native pw_rpc channel client for the MCU RPC channel."""
        return self.rpc_client.channel(protocol.HDLC_ADDRESS_RPC)

    @property
    def rpcs(self) -> Any:
        """Return Pigweed's native pw_rpc service tree on the MCU RPC channel."""
        return self.rpc_channel.rpcs

    async def invoke_rpc(
        self,
        rpc_method: Any,
        request: ProtobufMessage,
        *,
        timeout: float | None = None,
    ) -> bool | ProtobufMessage:
        """Invoke a typed Pigweed unary or server-streaming RPC directly on pw_rpc.Client."""
        if not self.serial or not self.serial.is_open:
            return False
        method_desc = rpc_method.method
        method_name = str(method_desc.full_name)
        if method_desc.type is PwRpcMethod.Type.SERVER_STREAMING:
            return await self._invoke_server_stream_rpc(rpc_method, request, method_name)
        effective_timeout = timeout if timeout is not None else self._response_timeout
        return await self._invoke_rpc_with_retries(rpc_method, request, method_name, effective_timeout)

    async def send_console(self, data: bytes) -> bool:
        """Transmit raw console bytes over the dedicated Pigweed HDLC console channel."""
        if not self.serial or not self.serial.is_open:
            return False
        return await self._write_hdlc_frame(
            protocol.HDLC_ADDRESS_CONSOLE,
            data,
            encrypt=True,
            label=_CONSOLE_WRITE_SPEC.name,
        )

    def queue_rpc_packet(self, packet: bytes) -> None:
        """Schedule upstream Pigweed output and record only client routing direction."""
        try:
            rpc_packet = pw_packets.decode(packet)
        except (ProtobufDecodeError, ValueError) as exc:
            logger.error("Unable to inspect outgoing Pigweed RPC packet", error=str(exc))
            rpc_packet = None

        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        loop = self._event_loop
        if loop is None or loop.is_closed():
            if current_loop is None:
                raise RuntimeError(
                    "Pigweed RPC output called outside the asyncio event loop; no active loop is registered"
                )
            loop = current_loop
            self._event_loop = loop

        if current_loop is loop:
            self._queue_rpc_packet_on_loop(packet, rpc_packet)
        else:
            loop.call_soon_threadsafe(self._queue_rpc_packet_on_loop, packet, rpc_packet)

    def _queue_rpc_packet_on_loop(self, packet: bytes, rpc_packet: RpcPacket | None) -> None:
        """Update client routing direction and create the serial writer task."""
        if rpc_packet is not None:
            packet_key = self._rpc_packet_key(rpc_packet)
            if rpc_packet.type == PacketType.REQUEST:
                self._active_client_route_keys.add(packet_key)
            elif rpc_packet.type == PacketType.CLIENT_ERROR:
                self._active_client_route_keys.discard(packet_key)

        task = asyncio.create_task(self._rpc_packet_writer(packet), name="mcubridge-pw-rpc-write")
        self._rpc_write_tasks.add(task)

        def _finish(completed: asyncio.Task[bool]) -> None:
            self._rpc_write_tasks.discard(completed)
            try:
                written = completed.result()
            except (asyncio.CancelledError, OSError, RuntimeError) as exc:
                logger.error("Pigweed RPC packet write task failed", error=str(exc))
                return
            if not written:
                logger.error("Pigweed RPC packet could not be written to serial")

        task.add_done_callback(_finish)

    async def _write_hdlc_frame(
        self,
        address: int,
        payload: bytes,
        *,
        encrypt: bool,
        allow_during_baud_negotiation: bool = False,
        label: str = "",
    ) -> bool:
        """Apply flow-control gating, optional AEAD encryption, HDLC framing, and serial TX."""
        serial_port = self.serial
        if not serial_port or not getattr(serial_port, "is_open", True):
            return False

        if not self.state.serial_tx_allowed.is_set() and not (
            allow_during_baud_negotiation and self.state.connection_fsm.negotiating_baud.is_active
        ):
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

        async with self._serial_write_lock:
            cipher = self.state.link_cipher
            if encrypt and self.config.serial_shared_secret and cipher is None:
                logger.error("Refusing unprotected serial frame before link authentication", label=label)
                return False

            frame_payload = cipher.encrypt(address, payload) if encrypt and cipher is not None else payload
            encoded = ui_frame(address, frame_payload)

            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[SERIAL -> MCU] [OP:%s] [HDLC]: [%s]",
                    label,
                    encoded.hex().upper(),
                )

            write_started = time.monotonic()
            try:
                await asyncio.to_thread(_write_and_flush, serial_port, encoded)
            except (AttributeError, OSError, RuntimeError, ValueError) as exc:
                self.state.metrics.serial_failures += 1
                logger.error("Serial write failed", label=label, error=str(exc))
                return False

        self.state.metrics.serial_latency_ms = (time.monotonic() - write_started) * 1000.0
        self.state.metrics.serial_bytes_sent += len(encoded)
        self.state.metrics.serial_frames_sent += 1
        return True

    async def _write_rpc_packet(self, packet: bytes) -> bool:
        """Apply link security and HDLC framing to a packet produced by pw_rpc.Client."""
        return await self._write_hdlc_frame(
            protocol.HDLC_ADDRESS_RPC,
            packet,
            encrypt=True,
            allow_during_baud_negotiation=True,
            label="pw_rpc",
        )

    def _get_transfer_manager(self) -> PwTransferManager:
        """Return the single upstream pw_transfer manager for the MCU RPC channel."""
        if self._transfer_manager is None:
            transfer_service = self.rpc_client.channel(protocol.HDLC_ADDRESS_RPC).rpcs.pw.transfer.Transfer
            self._transfer_manager = PwTransferManager(
                transfer_service,
                default_response_timeout_s=2.0,
                initial_response_timeout_s=5.0,
                max_retries=3,
                max_lifetime_retries=60,
                max_chunk_size_bytes=_TRANSFER_CHUNK_SIZE_BYTES,
            )
        return self._transfer_manager

    async def _run_transfer_in_worker[T](self, operation: Callable[[PwTransferManager], T]) -> T:
        """Keep the transfer lock until a blocking Pigweed operation really exits."""
        async with self._transfer_lock:
            if self.serial is None or not self.serial.is_open:
                raise ConnectionError("MCU transfer requested while the serial link is disconnected")
            manager = self._get_transfer_manager()
            worker = asyncio.create_task(asyncio.to_thread(operation, manager))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # asyncio cannot stop a running thread. Wait for the Manager operation
                # to finish before releasing the lock, otherwise a second transfer
                # could overlap the first on the AVR's single-role transfer thread.
                try:
                    await asyncio.shield(worker)
                except (PwTransferError, ConnectionError, OSError, RuntimeError, ValueError) as exc:
                    logger.debug("pw_transfer worker ended after caller cancellation", error=str(exc))
                raise

    async def transfer_read(self, resource_id: int, *, offset: int = 0) -> bytes:
        """Read an MCU transfer resource without blocking the asyncio serial loop."""
        return await self._run_transfer_in_worker(lambda mgr: mgr.read(resource_id, initial_offset=offset))

    async def transfer_write(self, resource_id: int, data: bytes, *, offset: int = 0) -> None:
        """Write an MCU transfer resource without blocking the asyncio serial loop."""
        await self._run_transfer_in_worker(lambda mgr: mgr.write(resource_id, data, initial_offset=offset))

    async def _invoke_unary_once(
        self,
        rpc_method: Any,
        request: ProtobufMessage,
        method_name: str,
        timeout: float,
    ) -> bool | ProtobufMessage:
        """Invoke a single unary pw_rpc call on the event loop; raises TimeoutError on timeout."""
        completion: asyncio.Future[bool | ProtobufMessage] = asyncio.get_running_loop().create_future()

        def _on_completed(call: UnaryCall, status: Status) -> None:
            if not completion.done():
                if status.ok():
                    response = call.response
                    completion.set_result(response if response is not None else True)
                else:
                    completion.set_result(False)

        def _on_error(_call: UnaryCall, error: Any) -> None:
            if not completion.done():
                self.state.metrics.serial_failures += 1
                logger.warning("Pigweed unary RPC failed", method=method_name, error=str(error))
                completion.set_result(False)

        rpc_started = time.monotonic()
        try:
            call = rpc_method.invoke(request, on_completed=_on_completed, on_error=_on_error)
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            self.state.metrics.serial_failures += 1
            logger.error("Unable to invoke Pigweed unary RPC", method=method_name, error=str(exc))
            return False

        self._active_rpc_calls.add(call)
        try:
            async with asyncio.timeout(timeout):
                result = await completion
            self.state.metrics.rpc_latency_ms = (time.monotonic() - rpc_started) * 1000.0
            return result
        except TimeoutError:
            cancel_rpc_call(call, "Pigweed RPC cancellation raced with completion", method=method_name)
            raise
        except asyncio.CancelledError:
            cancel_rpc_call(call, "Pigweed RPC cancellation raced with task cancellation", method=method_name)
            raise
        finally:
            self._active_rpc_calls.discard(call)

    async def _invoke_rpc_with_retries(
        self,
        rpc_method: Any,
        request: ProtobufMessage,
        method_name: str,
        timeout: float,
    ) -> bool | ProtobufMessage:
        def _before_sleep(_retry_state: tenacity.RetryCallState) -> None:
            self.state.metrics.serial_retries += 1

        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(self.max_attempts),
            wait=tenacity.wait_exponential(
                multiplier=SERIAL_HANDSHAKE_BACKOFF_BASE,
                max=SERIAL_HANDSHAKE_BACKOFF_MAX,
            ),
            retry=tenacity.retry_if_exception_type(TimeoutError),
            before_sleep=_before_sleep,
            reraise=False,
        )

        async def _attempt() -> bool | ProtobufMessage:
            return await self._invoke_unary_once(rpc_method, request, method_name, timeout)

        try:
            return await retryer(_attempt)
        except tenacity.RetryError:
            self.state.metrics.serial_failures += 1
            logger.warning("Pigweed unary RPC timed out", method=method_name, attempts=self.max_attempts)
            return False

    async def _invoke_server_stream_rpc(
        self,
        rpc_method: Any,
        request: ProtobufMessage,
        method_name: str,
    ) -> bool:
        """Invoke or update a server-streaming RPC via pw_rpc.Client."""
        rejection_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def _finish_stream_call(ok: bool, detail: Any) -> None:
            self._stream_call = None
            if not ok and not rejection_future.done():
                logger.warning("Pigweed server-streaming RPC failed", method=method_name, detail=str(detail))
                rejection_future.set_result(False)

        def _on_next(_call: Any, message: ProtobufMessage) -> None:
            self._enqueue_stream_event(message)

        def _on_completed(_call: Any, status: Any) -> None:
            _finish_stream_call(status.ok(), status)

        def _on_error(_call: Any, error: Any) -> None:
            _finish_stream_call(False, error)

        try:
            call = rpc_method.invoke(
                request,
                on_next=_on_next,
                on_completed=_on_completed,
                on_error=_on_error,
            )
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            logger.error("Unable to invoke Pigweed server-streaming RPC", method=method_name, error=str(exc))
            return False

        if not rejection_future.done():
            self._stream_call = call

        try:
            async with asyncio.timeout(min(self._ack_timeout, 0.05)):
                return await rejection_future
        except TimeoutError:
            return True

    async def send_rpc_message(
        self,
        operation: str | protocol.OperationSpec | tuple[int, int],
        payload: bytes | ProtobufMessage = b"",
        call_id: int | None = None,
        *,
        status: Status | None = None,
    ) -> bool:
        """Encode a descriptor-typed Pigweed request or response and send it over HDLC.

        Raw non-empty payloads and unknown service/method IDs are deliberately
        rejected. Empty bytes are accepted only as shorthand for an empty
        Protobuf request; responses must be typed and explicitly correlated.
        """
        spec = get_operation(operation)
        if status is not None:
            if call_id is None:
                logger.error("Refusing to send an RPC status without the incoming call_id", operation=str(operation))
                return False
            if spec is not None and spec.service_id is not None and spec.method_id is not None:
                service_id, method_id = spec.service_id, spec.method_id
            elif isinstance(operation, tuple):
                service_id, method_id = operation
            else:
                logger.error("Refusing RPC status without registered service/method IDs", operation=str(operation))
                return False
            rpc_ids = RpcIds(protocol.HDLC_ADDRESS_RPC, service_id, method_id, call_id)
            packet = (
                pw_packets.encode_response(rpc_ids, status=Status.OK)
                if status.ok()
                else pw_packets.encode_server_error(rpc_ids, status)
            )
            return await self._rpc_packet_writer(packet)

        if not self.serial:
            return False

        if spec is None or spec.service_id is None or spec.method_id is None:
            logger.error("Refusing RPC transmission without a registered Pigweed method", operation=str(operation))
            return False

        if isinstance(payload, ProtobufMessage):
            message = payload
        elif not payload and spec.request_type is not None:
            message = spec.request_type()
        else:
            logger.error(
                "Refusing untyped RPC payload; use a generated Protobuf message",
                operation=spec.name,
                payload_bytes=len(payload),
            )
            return False

        is_request = spec.request_type is not None and isinstance(message, spec.request_type)
        is_response = (
            spec.response_type is not None
            and spec.response_type is not spec.request_type
            and isinstance(message, spec.response_type)
        )
        if not (is_request or is_response):
            logger.error(
                "RPC message type does not match the Pigweed method descriptor",
                operation=spec.name,
                message_type=type(message).__name__,
            )
            return False

        if is_response and call_id is None:
            logger.error(
                "Refusing to infer an MCU RPC call_id from service/method; pass the incoming call_id explicitly",
                operation=spec.name,
            )
            return False

        if call_id is None:
            self.tx_sequence_id = (self.tx_sequence_id + 1) & protocol.UINT16_MAX
            call_id = self.tx_sequence_id

        rpc_ids = RpcIds(protocol.HDLC_ADDRESS_RPC, spec.service_id, spec.method_id, call_id)
        if is_response and spec.is_server_streaming:
            packet = pw_packets.encode_server_stream(rpc_ids, message)
        elif is_response:
            packet = pw_packets.encode_response(rpc_ids, message)
        else:
            packet = pw_packets.encode_request(rpc_ids, message)

        is_link_reset = spec is _LINK_RESET_SPEC
        is_link_sync = spec is _LINK_SYNC_SPEC
        if self.config.serial_shared_secret and self.state.link_cipher is not None and is_link_sync:
            return False

        return await self._write_hdlc_frame(
            protocol.HDLC_ADDRESS_RPC,
            packet,
            encrypt=not (is_link_reset or is_link_sync),
            label=spec.name,
        )

    async def negotiate_baudrate(self, target_baud: int) -> bool:
        payload = pb.SetBaudratePacket(baudrate=target_baud)
        was_synchronized = self.state.is_synchronized
        self.state.connection_fsm.start_negotiating()
        try:
            if not await self.invoke_rpc(
                self.rpcs.rpc.pb.SystemService.SetBaudrate,
                payload,
                timeout=SERIAL_BAUDRATE_NEGOTIATION_TIMEOUT,
            ):
                return False
            self.switch_local_baudrate(target_baud)
            await asyncio.sleep(protocol.BAUDRATE_CHANGE_DELAY_MS / 1000)
            if was_synchronized:
                self.state.connection_fsm.synchronize()
            return True
        except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
            logger.error("Baudrate negotiation failed", error=str(exc))
            return False
        finally:
            with anyio.CancelScope(shield=True):
                if self.state.connection_fsm.negotiating_baud.is_active:
                    self.state.connection_fsm.connect()
