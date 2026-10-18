"""Local gRPC Service implementation for local MPU clients over UNIX domain socket. [SIL-2]"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

import anyio
from anyio.streams.memory import MemoryObjectSendStream
import structlog
import structlog.contextvars
from google.protobuf.message import DecodeError as ProtobufDecodeError
from google.protobuf.message import Message as ProtobufMessage
from grpclib.server import Stream

from ..config.const import MCU_FS_PREFIX
from ..protocol import mcubridge_pb2 as pb
from ..protocol.mcubridge_grpc import LocalBridgeBase
from ..protocol.protocol import (
    AEAD_NONCE_SIZE,
    BOOTLOADER_MAGIC,
    MAX_DATASTORE_KEY_LENGTH,
    MAX_DATASTORE_VALUE_SIZE,
    REGISTRY,
    Command,
)
from ..protocol.structures import is_command_allowed

__all__ = [
    "ConsoleQueueTarget",
    "LocalBridgeService",
    "MemoryQueueAdapter",
    "parse_serial_response",
]

logger = structlog.get_logger("mcubridge.service.local_bridge")


class ConsoleQueueTarget(Protocol):
    def put_nowait(self, item: pb.CloudQueuedPublish) -> None: ...
    async def put(self, item: pb.CloudQueuedPublish) -> None: ...


class MemoryQueueAdapter:
    """Adapts an AnyIO MemoryObjectSendStream to queue-compatible API. [SIL-2]"""

    __slots__ = ("_send_stream",)
    _send_stream: MemoryObjectSendStream[pb.CloudQueuedPublish]

    def __init__(self, send_stream: MemoryObjectSendStream[pb.CloudQueuedPublish]) -> None:
        self._send_stream = send_stream

    def put_nowait(self, item: pb.CloudQueuedPublish) -> None:
        try:
            self._send_stream.send_nowait(item)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            logger.debug("Console stream subscriber disconnected during put_nowait", error=str(exc))
        except anyio.WouldBlock as exc:
            logger.warning("Console stream subscriber buffer full, dropping message", error=str(exc))

    async def put(self, item: pb.CloudQueuedPublish) -> None:
        try:
            await self._send_stream.send(item)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            logger.debug("Console stream subscriber disconnected during put", error=str(exc))

    def close(self) -> None:
        self._send_stream.close()


def parse_serial_response[T: ProtobufMessage](res: Any, target_type: type[T], default: T) -> T:
    """Safely decode serial response payload to target Protobuf message type. [SIL-2]"""
    if isinstance(res, target_type):
        return res
    if isinstance(res, (bytes, bytearray)):
        try:
            return target_type.FromString(bytes(res))
        except (ProtobufDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed to decode serial response into protobuf",
                target=target_type.__name__,
                error=str(exc),
            )
            return default
    return default


class LocalBridgeRuntime(Protocol):
    """Protocol for runtime link dependency in LocalBridgeService."""

    @property
    def state(self) -> Any: ...
    @property
    def serial(self) -> Any: ...
    @property
    def gpio(self) -> Any: ...
    @property
    def console_queues(self) -> list[ConsoleQueueTarget]: ...
    @property
    def ipc_requests(self) -> dict[bytes, asyncio.Queue[pb.CloudQueuedPublish]]: ...
    async def store_and_publish_datastore_value(self, key: str, value: bytes, /) -> None: ...
    async def safe_file_write(self, p_str: str, data: bytes, /) -> bool: ...
    async def safe_file_read(self, p_str: str, /) -> bytes | None: ...
    async def safe_file_remove(self, p_str: str, /) -> bool: ...
    async def run_process(self, command: str, /) -> int: ...
    async def poll_process(self, pid: int, /) -> pb.ProcessPollResponse: ...
    async def kill_process(self, pid: int, /) -> tuple[bool, str | None]: ...
    async def handle_request(self, inbound: Any, /) -> None: ...


class LocalBridgeService(LocalBridgeBase):
    """Implementation of the Local gRPC service for local MPU clients and Cloud Gateway forwarding."""

    def __init__(self, runtime_service: LocalBridgeRuntime) -> None:
        self.runtime_service = runtime_service

    # --- Core Business Logic Execution (Zero-Duplication) ---

    async def _send_serial_ack(self, cmd: Command, payload: ProtobufMessage | bytes) -> pb.GenericResponse:
        serial = self.runtime_service.serial
        res = (await serial.send(cmd.value, payload)) if serial else None
        return pb.GenericResponse(status="ok" if res is not None and res is not False else "error")

    async def _send_serial_query[T: ProtobufMessage](
        self, cmd: Command, payload: ProtobufMessage | bytes, resp_type: type[T]
    ) -> T:
        serial = self.runtime_service.serial
        res = (await serial.send(cmd.value, payload)) if serial else None
        return parse_serial_response(res, resp_type, resp_type())

    async def execute_set_pin_mode(self, req: pb.PinMode) -> pb.GenericResponse:
        return await self._send_serial_ack(Command.CMD_SET_PIN_MODE, req)

    async def execute_digital_write(self, req: pb.DigitalWrite) -> pb.GenericResponse:
        return await self._send_serial_ack(Command.CMD_DIGITAL_WRITE, req)

    async def execute_digital_read(self, req: pb.PinRead) -> pb.DigitalReadResponse:
        return await self._send_serial_query(Command.CMD_DIGITAL_READ, req, pb.DigitalReadResponse)

    async def execute_analog_write(self, req: pb.AnalogWrite) -> pb.GenericResponse:
        return await self._send_serial_ack(Command.CMD_ANALOG_WRITE, req)

    async def execute_analog_read(self, req: pb.PinRead) -> pb.AnalogReadResponse:
        return await self._send_serial_query(Command.CMD_ANALOG_READ, req, pb.AnalogReadResponse)

    async def execute_pin_subscribe(self, req: pb.PinSubscribeRequest) -> pb.PinSubscribeResponse:
        try:
            mode_str = pb.PinModeType.Name(req.mode).removeprefix("PIN_")
        except (ValueError, KeyError) as exc:
            logger.warning("Unrecognized pin mode enum, defaulting to INPUT", mode=req.mode, error=str(exc))
            mode_str = "INPUT"
        res = await self.runtime_service.gpio.subscribe_pin(
            pin=req.pin, mode=mode_str, interval_ms=req.interval_ms, hysteresis=req.hysteresis, enabled=req.enabled
        )
        return pb.PinSubscribeResponse(pin=req.pin, success=bool(res.get("status") == "ok"))

    async def execute_datastore_put(self, req: pb.DatastorePut) -> pb.GenericResponse:
        if (
            not req.key
            or len(req.key.encode("utf-8")) > MAX_DATASTORE_KEY_LENGTH
            or len(req.value) > MAX_DATASTORE_VALUE_SIZE
        ):
            return pb.GenericResponse(status="error")
        await self.runtime_service.store_and_publish_datastore_value(req.key, req.value)
        return pb.GenericResponse(status="ok")

    async def execute_datastore_get(self, req: pb.DatastoreGet) -> pb.DatastoreGetResponse:
        c = self.runtime_service.state.datastore_cache
        val = (await c.get(req.key, b"")) if c else b""
        return pb.DatastoreGetResponse(value=val or b"")

    async def execute_mailbox_push(self, req: pb.MailboxPush) -> pb.GenericResponse:
        await self.runtime_service.state.mailbox_queue.append(req.data)
        serial = self.runtime_service.serial
        if serial:
            await serial.send(Command.CMD_MAILBOX_PUSH.value, req)
        return pb.GenericResponse(status="ok")

    async def execute_mailbox_read(self, _req: pb.SubscribeRequest) -> pb.MailboxReadResponse:
        q = self.runtime_service.state.mailbox_incoming_queue
        val = await q.popleft() if len(q) > 0 else b""
        return pb.MailboxReadResponse(content=val or b"")

    async def _execute_file_mutation(
        self,
        request: pb.FileWrite | pb.FileRemove,
        cmd: Command,
        mcu_msg: ProtobufMessage,
        local_op: Callable[[], Coroutine[Any, Any, bool]],
        error_msg: str,
    ) -> pb.GenericResponse:
        clean_path = request.path.lstrip("/")
        if clean_path.startswith(MCU_FS_PREFIX):
            serial = self.runtime_service.serial
            ok = bool(await serial.send(cmd.value, mcu_msg)) if serial else False
            return pb.GenericResponse(status="ok" if ok else "error")
        if await local_op():
            return pb.GenericResponse(status="ok")
        return pb.GenericResponse(status="error", message=error_msg)

    async def execute_file_write(self, req: pb.FileWrite) -> pb.GenericResponse:
        clean = req.path.lstrip("/")
        mcu_path = clean.removeprefix(MCU_FS_PREFIX) if clean.startswith(MCU_FS_PREFIX) else req.path
        return await self._execute_file_mutation(
            req,
            Command.CMD_FILE_WRITE,
            pb.FileWrite(path=mcu_path, data=req.data),
            lambda: self.runtime_service.safe_file_write(req.path, req.data),
            "Path not allowed or quota exceeded",
        )

    async def execute_file_read(self, req: pb.FileRead) -> pb.FileReadResponse:
        clean = req.path.lstrip("/")
        if clean.startswith(MCU_FS_PREFIX):
            content = await self.runtime_service.read_mcu_file(clean)
            return pb.FileReadResponse(content=content)
        content = await self.runtime_service.safe_file_read(req.path)
        return pb.FileReadResponse(content=content or b"")

    async def execute_file_remove(self, req: pb.FileRemove) -> pb.GenericResponse:
        clean = req.path.lstrip("/")
        mcu_path = clean.removeprefix(MCU_FS_PREFIX) if clean.startswith(MCU_FS_PREFIX) else req.path
        return await self._execute_file_mutation(
            req,
            Command.CMD_FILE_REMOVE,
            pb.FileRemove(path=mcu_path),
            lambda: self.runtime_service.safe_file_remove(req.path),
            "Path not allowed or not found",
        )

    async def execute_process_run_async(self, req: pb.ProcessRunAsync) -> pb.ProcessRunAsyncResponse:
        allowed = is_command_allowed(self.runtime_service.state.allowed_policy, req.command)
        pid = (await self.runtime_service.run_process(req.command)) if (req.command and allowed) else 0
        return pb.ProcessRunAsyncResponse(pid=pid or 0)

    async def execute_process_poll(self, req: pb.ProcessPoll) -> pb.ProcessPollResponse:
        return await self.runtime_service.poll_process(req.pid)

    async def execute_process_kill(self, req: pb.ProcessKill) -> pb.GenericResponse:
        ok, err = await self.runtime_service.kill_process(req.pid)
        return (
            pb.GenericResponse(status="ok")
            if ok
            else pb.GenericResponse(status="error", message=err or "PID not found")
        )

    async def execute_spi_transfer(self, req: pb.SpiTransfer) -> pb.SpiTransferResponse:
        return await self._send_serial_query(Command.CMD_SPI_TRANSFER, req, pb.SpiTransferResponse)

    async def execute_spi_configure(self, req: pb.SpiConfig) -> pb.GenericResponse:
        serial = self.runtime_service.serial
        ok = bool(
            serial
            and await serial.send(Command.CMD_SPI_BEGIN.value, b"")
            and await serial.send(Command.CMD_SPI_SET_CONFIG.value, req)
        )
        return pb.GenericResponse(status="ok" if ok else "error")

    async def execute_get_version(self, _req: pb.SubscribeRequest) -> pb.VersionResponse:
        resp = await self._send_serial_query(Command.CMD_GET_VERSION, b"", pb.VersionResponse)
        if resp.major or resp.minor or resp.patch:
            self.runtime_service.state.mcu_version = (resp.major, resp.minor, resp.patch)
        return resp

    async def execute_get_free_memory(self, _req: pb.SubscribeRequest) -> pb.FreeMemoryResponse:
        return await self._send_serial_query(Command.CMD_GET_FREE_MEMORY, b"", pb.FreeMemoryResponse)

    async def execute_get_status(self, _req: pb.SubscribeRequest) -> pb.BridgeStatus:
        return self.runtime_service.state.build_status_snapshot()

    async def execute_enter_bootloader(self, req: pb.EnterBootloader) -> pb.GenericResponse:
        serial = self.runtime_service.serial
        magic = req.magic or BOOTLOADER_MAGIC
        ok = bool(serial and await serial.send(Command.CMD_ENTER_BOOTLOADER.value, pb.EnterBootloader(magic=magic)))
        return pb.GenericResponse(status="ok" if ok else "error")

    async def execute_console_write(self, req: pb.ConsoleWrite) -> pb.GenericResponse:
        if pl := req.data:
            self.runtime_service.state.console_to_mcu_queue.append(pl)
            await self.runtime_service.flush_console_queue()
        return pb.GenericResponse(status="ok")

    async def execute_clock_sync(self, req: pb.ClockSyncRequest) -> pb.ClockSyncResponse:
        return await self._send_serial_query(Command.CMD_CLOCK_SYNC, req, pb.ClockSyncResponse)

    async def execute_publish(self, req: pb.CloudQueuedPublish) -> pb.CloudQueuedPublish:
        has_correlation = req.HasField("correlation_data")
        route = REGISTRY.parse_topic(self.runtime_service.state.cloud_topic_prefix, req.topic_name)
        is_query = has_correlation or (route is not None and route.direction == "req")

        correlation = (
            req.correlation_data if has_correlation else (secrets.token_bytes(AEAD_NONCE_SIZE) if is_query else b"")
        )

        with structlog.contextvars.bound_contextvars(
            topic=req.topic_name, correlation=correlation.hex() if correlation else ""
        ):
            response_queue: asyncio.Queue[pb.CloudQueuedPublish] | None = None
            if is_query and correlation:
                response_queue = asyncio.Queue(maxsize=1)
                self.runtime_service.ipc_requests[correlation] = response_queue

            try:
                await self.runtime_service.handle_request(
                    pb.CloudQueuedPublish(topic_name=req.topic_name, payload=req.payload, correlation_data=correlation)
                )

                if is_query and response_queue is not None:
                    try:
                        with anyio.fail_after(15.0):
                            return await response_queue.get()
                    except TimeoutError:
                        logger.warning("IPC request timed out")
                        return pb.CloudQueuedPublish()
                return pb.CloudQueuedPublish()
            finally:
                with anyio.CancelScope(shield=True):
                    if is_query and correlation:
                        self.runtime_service.ipc_requests.pop(correlation, None)

    async def execute_rpc(self, method_name: str, payload_bytes: bytes) -> bytes:
        """[SIL-2] Dispatch and execute an RPC by name, returning serialized response."""
        spec = REGISTRY.get(method_name)
        if not spec or not spec.request_type or not spec.handler_name:
            raise ValueError(f"Unknown RPC method: {method_name}")
        req = spec.request_type()
        if payload_bytes:
            req.ParseFromString(payload_bytes)
        handler = getattr(self, spec.handler_name)
        with structlog.contextvars.bound_contextvars(rpc_method=method_name):
            resp = await handler(req)
            return resp.SerializeToString()

    async def _handle_unary[ReqT: ProtobufMessage, RespT: ProtobufMessage](
        self,
        stream: Stream[ReqT, RespT],
        rpc_name: str,
        handler: Callable[[ReqT], Coroutine[Any, Any, RespT]],
    ) -> None:
        """[SIL-2] Generic handler executing unary gRPC call with bound contextvars."""
        if (req := await stream.recv_message()) is not None:
            with structlog.contextvars.bound_contextvars(rpc=rpc_name):
                await stream.send_message(await handler(req))

    # --- LocalBridgeBase Stream Handlers (with structlog contextvars propagation) ---

    async def SetPinMode(self, stream: Stream[pb.PinMode, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "SetPinMode", self.execute_set_pin_mode)

    async def DigitalWrite(self, stream: Stream[pb.DigitalWrite, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "DigitalWrite", self.execute_digital_write)

    async def DigitalRead(self, stream: Stream[pb.PinRead, pb.DigitalReadResponse]) -> None:
        await self._handle_unary(stream, "DigitalRead", self.execute_digital_read)

    async def AnalogWrite(self, stream: Stream[pb.AnalogWrite, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "AnalogWrite", self.execute_analog_write)

    async def AnalogRead(self, stream: Stream[pb.PinRead, pb.AnalogReadResponse]) -> None:
        await self._handle_unary(stream, "AnalogRead", self.execute_analog_read)

    async def PinSubscribe(self, stream: Stream[pb.PinSubscribeRequest, pb.PinSubscribeResponse]) -> None:
        await self._handle_unary(stream, "PinSubscribe", self.execute_pin_subscribe)

    async def DatastorePut(self, stream: Stream[pb.DatastorePut, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "DatastorePut", self.execute_datastore_put)

    async def DatastoreGet(self, stream: Stream[pb.DatastoreGet, pb.DatastoreGetResponse]) -> None:
        await self._handle_unary(stream, "DatastoreGet", self.execute_datastore_get)

    async def MailboxPush(self, stream: Stream[pb.MailboxPush, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "MailboxPush", self.execute_mailbox_push)

    async def MailboxRead(self, stream: Stream[pb.SubscribeRequest, pb.MailboxReadResponse]) -> None:
        await self._handle_unary(stream, "MailboxRead", self.execute_mailbox_read)

    async def FileWrite(self, stream: Stream[pb.FileWrite, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "FileWrite", self.execute_file_write)

    async def FileRead(self, stream: Stream[pb.FileRead, pb.FileReadResponse]) -> None:
        await self._handle_unary(stream, "FileRead", self.execute_file_read)

    async def FileRemove(self, stream: Stream[pb.FileRemove, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "FileRemove", self.execute_file_remove)

    async def ProcessRunAsync(self, stream: Stream[pb.ProcessRunAsync, pb.ProcessRunAsyncResponse]) -> None:
        await self._handle_unary(stream, "ProcessRunAsync", self.execute_process_run_async)

    async def ProcessPoll(self, stream: Stream[pb.ProcessPoll, pb.ProcessPollResponse]) -> None:
        await self._handle_unary(stream, "ProcessPoll", self.execute_process_poll)

    async def ProcessKill(self, stream: Stream[pb.ProcessKill, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "ProcessKill", self.execute_process_kill)

    async def SpiTransfer(self, stream: Stream[pb.SpiTransfer, pb.SpiTransferResponse]) -> None:
        await self._handle_unary(stream, "SpiTransfer", self.execute_spi_transfer)

    async def SpiConfigure(self, stream: Stream[pb.SpiConfig, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "SpiConfigure", self.execute_spi_configure)

    async def GetVersion(self, stream: Stream[pb.SubscribeRequest, pb.VersionResponse]) -> None:
        await self._handle_unary(stream, "GetVersion", self.execute_get_version)

    async def GetFreeMemory(self, stream: Stream[pb.SubscribeRequest, pb.FreeMemoryResponse]) -> None:
        await self._handle_unary(stream, "GetFreeMemory", self.execute_get_free_memory)

    async def GetStatus(self, stream: Stream[pb.SubscribeRequest, pb.BridgeStatus]) -> None:
        await self._handle_unary(stream, "GetStatus", self.execute_get_status)

    async def EnterBootloader(self, stream: Stream[pb.EnterBootloader, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "EnterBootloader", self.execute_enter_bootloader)

    async def ConsoleWrite(self, stream: Stream[pb.ConsoleWrite, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, "ConsoleWrite", self.execute_console_write)

    async def ClockSync(self, stream: Stream[pb.ClockSyncRequest, pb.ClockSyncResponse]) -> None:
        await self._handle_unary(stream, "ClockSync", self.execute_clock_sync)

    async def Publish(self, stream: Stream[pb.CloudQueuedPublish, pb.CloudQueuedPublish]) -> None:
        if (req := await stream.recv_message()) is not None:
            try:
                await stream.send_message(await self.execute_publish(req))
            except OSError as exc:
                logger.debug("IPC connection closed during response write", error=str(exc))

    async def SubscribeConsole(self, stream: Stream[pb.SubscribeRequest, pb.CloudQueuedPublish]) -> None:
        if await stream.recv_message() is None:
            return

        send_stream, receive_stream = anyio.create_memory_object_stream[pb.CloudQueuedPublish](max_buffer_size=64)
        adapter = MemoryQueueAdapter(send_stream)
        self.runtime_service.console_queues.append(adapter)
        try:
            async with receive_stream:
                async for msg in receive_stream:
                    await stream.send_message(msg)
        except (OSError, RuntimeError) as e:
            logger.error("Local IPC console stream error", error=str(e))
            raise
        finally:
            adapter.close()
            if adapter in self.runtime_service.console_queues:
                self.runtime_service.console_queues.remove(adapter)
