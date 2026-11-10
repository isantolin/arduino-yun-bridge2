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
from pw_rpc.descriptors import Channel as PwRpcChannel, Method as PwRpcMethod
from pw_rpc.internal.packet_pb2 import PacketType
from pw_tokenizer import detokenize

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
_LINK_RESET_SPEC: Final = protocol.BY_NAME["rpc.pb.SystemService/LinkReset"]
_HANDSHAKE_RAW_NAMES: Final[frozenset[str]] = frozenset(
    {
        "rpc.pb.SystemService/LinkSync",
        "rpc.pb.SystemService/LinkReset",
    }
)
_STREAM_EVENT_QUEUE_MAX: Final[int] = 64

# Register wifi:// and tcp:// scheme aliases in pyserial
sys.modules["serial.urlhandler.protocol_wifi"] = protocol_socket
sys.modules["serial.urlhandler.protocol_tcp"] = protocol_socket


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
        self.consecutive_crc_errors = 0
        self.tx_sequence_id = 0

        self._serial_write_lock = asyncio.Lock()
        self._rpc_write_tasks: set[asyncio.Task[bool]] = set()
        self._active_rpc_calls: list[Any] = []
        self._active_rpc_packet_ids: set[tuple[int, int, int, int]] = set()
        self._active_server_call_ids: dict[tuple[int, int], int] = {}
        self._stream_call: Any | None = None
        self._stream_event_queue: deque[ProtobufMessage] = deque(maxlen=_STREAM_EVENT_QUEUE_MAX)
        self._stream_backpressure_drops: int = 0
        self.rpc_client = PwRpcClient(
            cast(Any, callback_client).Impl(),
            [PwRpcChannel(protocol.HDLC_ADDRESS_RPC, self.queue_rpc_packet)],
            protocol.PW_SERVICES,
        )

        self._ack_timeout = max(self.config.serial_retry_timeout or 0, float(protocol.DEFAULT_ACK_TIMEOUT_MS) / 1000.0)
        self._response_timeout = max(self.config.serial_response_timeout or 0, self._ack_timeout)
        self.max_attempts = max(1, self.config.serial_retry_attempts or 1)
        self.decoder = FrameDecoder()
        self.detokenizer = detokenize.Detokenizer()

    def switch_local_baudrate(self, target_baud: int) -> None:
        try:
            if self.serial:
                self.serial.baudrate = target_baud
                logger.info("Local UART switched baudrate", baud=target_baud)
        except (AttributeError, OSError, ValueError) as e:
            raise RuntimeError(f"UART access failed: {e}") from e

    async def reset(self) -> None:
        for call in tuple(self._active_rpc_calls):
            try:
                call.cancel()
            except (AttributeError, RuntimeError, ValueError) as exc:
                logger.debug("Unable to cancel pending Pigweed RPC during reset", error=str(exc))
        self.cancel_pin_stream()
        self._active_rpc_calls.clear()
        self._active_rpc_packet_ids.clear()
        self._active_server_call_ids.clear()
        self._stream_event_queue.clear()

    def cancel_pin_stream(self) -> bool:
        """Explicitly cancel any active server-streaming RPC call."""
        call = self._stream_call
        self._stream_call = None
        if call is None:
            return False
        try:
            call.cancel()
            return True
        except (AttributeError, KeyError, RuntimeError, ValueError) as exc:
            logger.debug("Unable to cancel Pigweed server-streaming RPC", error=str(exc))
            return False

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
                await self.service.handle_mcu_frame("ConsoleWrite", 0, pb.ConsoleWrite(data=frame_data))
            return

        if frame.address == protocol.HDLC_ADDRESS_RPC:
            try:
                pkt = pw_packets.decode(frame_data)
            except (ProtobufDecodeError, ValueError) as exc:
                self.state.serial_decode_errors += 1
                logger.error("Malformed RpcPacket received from MCU", error=str(exc), raw_hex=frame_data.hex())
                return
            spec = protocol.get_operation(pkt)
            seq_id = pkt.call_id

            if self.config.serial_shared_secret and self.state.link_cipher is None:
                is_link_sync_response = (
                    pkt.service_id == _LINK_SYNC_SPEC.service_id
                    and pkt.method_id == _LINK_SYNC_SPEC.method_id
                    and pkt.type in (PacketType.RESPONSE, PacketType.SERVER_ERROR)
                )
                if not is_link_sync_response:
                    self.state.serial_decode_errors += 1
                    logger.error("Dropping unprotected pre-handshake RPC frame")
                    return

            # Pigweed's native client owns responses and server streams for Linux-initiated RPCs,
            # including call-ID correlation, payload decoding, stream events, and completion status.
            rpc_packet_key = (pkt.channel_id, pkt.service_id, pkt.method_id, pkt.call_id)
            is_client_rpc = (
                spec is not None
                and spec.name not in _HANDSHAKE_RAW_NAMES
                and pkt.type in (PacketType.RESPONSE, PacketType.SERVER_ERROR, PacketType.SERVER_STREAM)
                and rpc_packet_key in self._active_rpc_packet_ids
            )
            if not pw_packets.for_server(pkt) and is_client_rpc:
                if pkt.type != PacketType.SERVER_STREAM:
                    self._active_rpc_packet_ids.discard(rpc_packet_key)
                self.rpc_client.process_packet(frame_data)
                await self._drain_stream_events()
                return

            # pw_rpc: a non-OK status (or SERVER_ERROR) is a failure regardless of payload;
            # successful responses map onto the operation's canonical service/method tuple.
            is_error_pkt = pkt.type == PacketType.SERVER_ERROR or (pkt.type == PacketType.RESPONSE and pkt.status != 0)
            is_status_pkt = pkt.method_id in SERIAL_FAILURE_STATUS_CODES or pkt.method_id in SERIAL_SUCCESS_STATUS_CODES
            dispatch_key: int | str | tuple[int, int]
            if is_error_pkt:
                dispatch_key = Status.ERROR.value
            elif spec is None and is_status_pkt:
                dispatch_key = pkt.method_id
            else:
                dispatch_key = (pkt.service_id, pkt.method_id)

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
                (spec is not None and spec.name in _HANDSHAKE_RAW_NAMES)
                or dispatch_key in SERIAL_FAILURE_STATUS_CODES
                or dispatch_key in SERIAL_SUCCESS_STATUS_CODES
            )
            if pkt.type == PacketType.REQUEST:
                self._active_server_call_ids[(pkt.service_id, pkt.method_id)] = pkt.call_id
            if self.service and (
                pkt.type != PacketType.RESPONSE
                or (is_response_handler and dispatch_key in getattr(self.service, "mcu_registry", {}))
            ):
                await self.service.handle_mcu_frame(dispatch_key, seq_id, payload)

    async def check_baudrate_fallback(self) -> None:
        self.consecutive_crc_errors += 1
        if self.consecutive_crc_errors >= self.config.serial_fallback_threshold:
            logger.error("Fallback to safe baudrate", baud=self.config.serial_safe_baud)
            self.consecutive_crc_errors = 0
            if self.config.serial_baud != self.config.serial_safe_baud:
                await self.negotiate_baudrate(self.config.serial_safe_baud)

    async def send(
        self,
        command_id: int | str | protocol.OperationSpec | tuple[int, int],
        payload: bytes | ProtobufMessage,
        seq_id: int | None = None,
        timeout: float | None = None,
    ) -> bool | bytes | ProtobufMessage:
        """Send an operation, delegating unary and server-streaming RPCs to Pigweed's native client."""
        if not self.serial or not self.serial.is_open:
            return False

        spec = get_operation(command_id)
        pw_method = (
            protocol.PW_SERVICES[spec.service_name].methods[spec.method_name]
            if spec is not None and spec.service_name and spec.method_name
            else None
        )
        is_streaming_response: bool = False
        if pw_method is not None and getattr(pw_method, "type", None) is PwRpcMethod.Type.SERVER_STREAMING:
            is_streaming_response = isinstance(payload, bytes) and payload == b""
        is_server_response: bool = (
            spec is not None
            and spec.response_type is not None
            and spec.response_type is not spec.request_type
            and (isinstance(payload, spec.response_type) or is_streaming_response)
        )
        if (
            spec is not None
            and not is_server_response
            and spec.service_name
            and spec.method_name
            and spec.name not in _HANDSHAKE_RAW_NAMES
        ):
            return await self._send_rpc(spec, payload, timeout)

        return await self.send_raw(spec if spec is not None else command_id, payload, seq_id)

    def queue_rpc_packet(self, packet: bytes) -> None:
        """Queue a Pigweed-generated RPC packet for encrypted HDLC transmission."""
        try:
            rpc_packet = pw_packets.decode(packet)
            if rpc_packet.type == PacketType.REQUEST:
                self._active_rpc_packet_ids.add(
                    (
                        rpc_packet.channel_id,
                        rpc_packet.service_id,
                        rpc_packet.method_id,
                        rpc_packet.call_id,
                    )
                )
            elif rpc_packet.type == PacketType.CLIENT_ERROR:
                self._active_rpc_packet_ids.discard(
                    (
                        rpc_packet.channel_id,
                        rpc_packet.service_id,
                        rpc_packet.method_id,
                        rpc_packet.call_id,
                    )
                )
        except (ProtobufDecodeError, ValueError) as exc:
            logger.error("Unable to decode outgoing Pigweed RPC packet", error=str(exc))
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("Pigweed RPC output invoked outside the asyncio event loop") from exc
        task = loop.create_task(self._write_rpc_packet(packet), name="mcubridge-pw-rpc-write")
        self._rpc_write_tasks.add(task)

        def _finish(completed: asyncio.Task[bool]) -> None:
            self._rpc_write_tasks.discard(completed)
            if not completed.result():
                logger.error("Pigweed RPC packet could not be written to serial")

        task.add_done_callback(_finish)

    async def _write_rpc_packet(self, packet: bytes) -> bool:
        """Apply link security and HDLC framing to a packet produced by pw_rpc.Client."""
        serial_port = self.serial
        if not serial_port or not serial_port.is_open:
            return False
        if not self.state.serial_tx_allowed.is_set() and not self.state.connection_fsm.negotiating_baud.is_active:
            try:
                async with asyncio.timeout(FLOW_CONTROL_WAIT_TIMEOUT_SECONDS):
                    await self.state.serial_tx_allowed.wait()
            except TimeoutError:
                logger.warning("Timed out waiting for serial TX flow control before RPC write")
                self.state.mcu_is_paused = False
                self.state.serial_tx_allowed.set()

        from pw_hdlc.encode import ui_frame

        async with self._serial_write_lock:
            cipher = self.state.link_cipher
            if self.config.serial_shared_secret and cipher is None:
                logger.error("Refusing Pigweed RPC output before link authentication")
                return False

            frame_payload = cipher.encrypt(protocol.HDLC_ADDRESS_RPC, packet) if cipher is not None else packet
            encoded = ui_frame(protocol.HDLC_ADDRESS_RPC, frame_payload)

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

    def _decode_rpc_request(self, spec: Any, payload: bytes | ProtobufMessage) -> ProtobufMessage | None:
        if spec.request_type is None:
            logger.error("Pigweed RPC descriptor has no request type", operation=spec.name)
            return None
        request_type = spec.request_type
        if isinstance(payload, request_type):
            return cast(ProtobufMessage, payload)
        if isinstance(payload, bytes):
            try:
                return cast(ProtobufMessage, request_type.FromString(payload))
            except ProtobufDecodeError as exc:
                logger.error("Unable to decode Pigweed RPC request payload", operation=spec.name, error=str(exc))
                return None
        logger.error("Invalid request type for Pigweed RPC", operation=spec.name)
        return None

    def _get_rpc_method(self, spec: Any) -> Any | None:
        method_name = f"{spec.service_name}/{spec.method_name}"
        try:
            return self.rpc_client.channel(protocol.HDLC_ADDRESS_RPC).method(method_name)
        except (KeyError, ValueError) as exc:
            logger.error(
                "Pigweed RPC method is not present in generated descriptors",
                method=method_name,
                error=str(exc),
            )
            return None

    async def _invoke_rpc_with_retries(
        self,
        rpc_method: Any,
        request: ProtobufMessage,
        method_name: str,
        timeout: float,
        *,
        is_finite_stream: bool = False,
    ) -> bool | ProtobufMessage:
        for attempt in range(1, self.max_attempts + 1):
            loop = asyncio.get_running_loop()
            completion: asyncio.Future[bool | ProtobufMessage] = loop.create_future()
            callback_state: dict[str, ProtobufMessage | None] = {"response": None}
            stream_chunks: list[bytes] = []

            def _on_next(
                _call: Any,
                message: ProtobufMessage,
                state: dict[str, ProtobufMessage | None] = callback_state,
                chunks: list[bytes] = stream_chunks,
            ) -> None:
                if is_finite_stream:
                    chunks.append(bytes(getattr(message, "content", b"")))
                else:
                    state["response"] = message

            def _on_completed(
                _call: Any,
                status: Any,
                future: asyncio.Future[bool | ProtobufMessage] = completion,
                state: dict[str, ProtobufMessage | None] = callback_state,
                chunks: list[bytes] = stream_chunks,
            ) -> None:
                if future.done():
                    return
                if status.ok():
                    if is_finite_stream:
                        future.set_result(pb.FileReadResponse(content=b"".join(chunks)))
                    else:
                        response_message = state["response"]
                        future.set_result(response_message if response_message is not None else True)
                else:
                    future.set_result(False)

            def _on_error(
                _call: Any,
                error: Any,
                future: asyncio.Future[bool | ProtobufMessage] = completion,
            ) -> None:
                if not future.done():
                    logger.warning("Pigweed unary RPC failed", method=method_name, error=str(error))
                    future.set_result(False)

            try:
                call = rpc_method.invoke(
                    request,
                    on_next=_on_next,
                    on_completed=_on_completed,
                    on_error=_on_error,
                )
            except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                logger.error("Unable to invoke Pigweed unary RPC", method=method_name, error=str(exc))
                return False

            self._active_rpc_calls.append(call)
            try:
                async with asyncio.timeout(timeout):
                    return await completion
            except TimeoutError:
                try:
                    call.cancel()
                except (KeyError, RuntimeError, ValueError) as exc:
                    logger.debug(
                        "Pigweed RPC cancellation raced with completion",
                        method=method_name,
                        error=str(exc),
                    )
                if attempt < self.max_attempts:
                    await asyncio.sleep(
                        min(SERIAL_HANDSHAKE_BACKOFF_BASE * (2 ** (attempt - 1)), SERIAL_HANDSHAKE_BACKOFF_MAX)
                    )
            except asyncio.CancelledError:
                try:
                    call.cancel()
                except (KeyError, RuntimeError, ValueError) as exc:
                    logger.debug(
                        "Pigweed RPC cancellation raced with task cancellation",
                        method=method_name,
                        error=str(exc),
                    )
                raise
            finally:
                if call in self._active_rpc_calls:
                    self._active_rpc_calls.remove(call)
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

        def _finish_stream_call(
            ok: bool,
            detail: Any,
            future: asyncio.Future[bool] = rejection_future,
        ) -> None:
            self._stream_call = None
            if not ok and not future.done():
                logger.warning("Pigweed server-streaming RPC failed", method=method_name, detail=str(detail))
                future.set_result(False)

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

    async def _send_rpc(
        self,
        spec: Any,
        payload: bytes | ProtobufMessage,
        timeout: float | None,
    ) -> bool | ProtobufMessage:
        """Invoke a unary or server-streaming method through pw_rpc.Client."""
        request = self._decode_rpc_request(spec, payload)
        if request is None:
            return False
        rpc_method = self._get_rpc_method(spec)
        if rpc_method is None:
            return False
        method_name = f"{spec.service_name}/{spec.method_name}"
        pw_method = protocol.PW_SERVICES[spec.service_name].methods[spec.method_name]
        effective_timeout = timeout if timeout is not None else self._response_timeout
        if pw_method.type is PwRpcMethod.Type.SERVER_STREAMING:
            if spec.response_type is pb.FileReadResponse:
                return await self._invoke_rpc_with_retries(
                    rpc_method,
                    request,
                    method_name,
                    effective_timeout,
                    is_finite_stream=True,
                )
            return await self._invoke_server_stream_rpc(rpc_method, request, method_name)
        return await self._invoke_rpc_with_retries(rpc_method, request, method_name, effective_timeout)

    async def send_raw(
        self,
        command_id: int | str | protocol.OperationSpec | tuple[int, int],
        payload: bytes | ProtobufMessage,
        seq_id: int | None = None,
    ) -> bool:
        """Low-level send logic without tracking."""
        if not self.serial:
            return False

        if not self.state.serial_tx_allowed.is_set():
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

        spec = get_operation(command_id)
        if seq_id is None:
            if (
                spec is not None
                and spec.service_id is not None
                and spec.method_id is not None
                and spec.response_type is not None
                and spec.response_type is not spec.request_type
                and (spec.service_id, spec.method_id) in self._active_server_call_ids
            ):
                key = (spec.service_id, spec.method_id)
                pw_method = (
                    protocol.PW_SERVICES[spec.service_name].methods[spec.method_name]
                    if spec.service_name and spec.method_name
                    else None
                )
                if (
                    pw_method is not None
                    and pw_method.type is PwRpcMethod.Type.SERVER_STREAMING
                    and isinstance(payload, spec.response_type)
                ):
                    seq_id = self._active_server_call_ids[key]
                else:
                    seq_id = self._active_server_call_ids.pop(key)
            else:
                self.tx_sequence_id = (self.tx_sequence_id + 1) & protocol.UINT16_MAX
                seq_id = self.tx_sequence_id

        async with self._serial_write_lock:
            serial_port = self.serial
            if not serial_port:
                return False

            cipher = self.state.link_cipher
            target = spec if spec is not None else command_id
            is_link_reset = spec is _LINK_RESET_SPEC
            is_link_sync = spec is _LINK_SYNC_SPEC
            is_console_write = (spec is not None and spec.name == "ConsoleWrite") or command_id == "ConsoleWrite"

            if is_console_write:
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

                    packet = protocol.encode_rpc_packet(target, payload, call_id=seq_id or 1)
                    envelope = cipher.encrypt(protocol.HDLC_ADDRESS_RPC, packet)
                    encoded = ui_frame(protocol.HDLC_ADDRESS_RPC, envelope)
                else:
                    encoded = protocol.encode_hdlc_rpc(target, payload, call_id=seq_id or 1)

            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[SERIAL -> MCU] [OP:%s] [HDLC]: [%s]",
                    spec.name if spec is not None else command_id,
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
        try:
            if not await self.send(
                "rpc.pb.SystemService/SetBaudrate",
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
