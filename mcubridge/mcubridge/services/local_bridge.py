"""Local gRPC Service implementation for local MPU clients over UNIX domain socket. [SIL-2]"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import anyio
from anyio.streams.memory import MemoryObjectSendStream
import structlog
import structlog.contextvars
from google.protobuf.message import DecodeError as ProtobufDecodeError
from google.protobuf.message import Message as ProtobufMessage
from grpclib.const import Handler
from grpclib.server import Stream

from ..protocol import mcubridge_pb2 as pb
from ..protocol.mcubridge_grpc import LocalBridgeBase
from ..protocol.protocol import (
    AEAD_NONCE_SIZE,
    BOOTLOADER_MAGIC,
    REGISTRY,
    Command,
    OperationSpec,
)

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
    async def datastore_put(
        self, _seq_or_key: int | str | pb.DatastorePut, value: bytes | None = None, *, reply_context: Any | None = None
    ) -> bool: ...
    async def datastore_get(self, key: str, /) -> bytes: ...
    async def mailbox_push(self, data: bytes, /) -> bool: ...
    async def mailbox_read(self) -> bytes: ...
    async def file_write(self, path: str, data: bytes, /) -> bool: ...
    async def file_read(self, path: str, /) -> bytes | None: ...
    async def file_remove(self, path: str, /) -> bool: ...
    async def safe_file_write(self, p_str: str, data: bytes, /) -> bool: ...
    async def safe_file_read(self, p_str: str, /) -> bytes | None: ...
    async def read_mcu_file(self, target: str, /) -> bytes: ...
    async def safe_file_remove(self, p_str: str, /) -> bool: ...
    async def flush_console_queue(self) -> None: ...
    async def run_process(self, command: str, /) -> int: ...
    async def poll_process(self, pid: int, /) -> pb.ProcessPollResponse: ...
    async def kill_process(
        self, _seq_or_pid: int | pb.ProcessKill, pid: int | None = None
    ) -> tuple[bool, str | None]: ...
    async def handle_request(self, inbound: Any, /) -> None: ...


class LocalBridgeService:
    """Implementation of the Local gRPC service for local MPU clients and Cloud Gateway forwarding."""

    def __init__(self, runtime_service: LocalBridgeRuntime) -> None:
        self.runtime_service = runtime_service

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def __mapping__(self) -> dict[str, Handler]:
        return REGISTRY.build_local_bridge_mapping(self)

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

    async def pin_subscribe(self, req: pb.PinSubscribeRequest) -> pb.PinSubscribeResponse:
        try:
            mode_str = pb.PinModeType.Name(req.mode).removeprefix("PIN_")
        except (ValueError, KeyError) as exc:
            logger.warning("Unrecognized pin mode enum, defaulting to INPUT", mode=req.mode, error=str(exc))
            mode_str = "INPUT"
        res = await self.runtime_service.gpio.subscribe_pin(
            pin=req.pin, mode=mode_str, interval_ms=req.interval_ms, hysteresis=req.hysteresis, enabled=req.enabled
        )
        return pb.PinSubscribeResponse(pin=req.pin, success=bool(res.get("status") == "ok"))

    async def datastore_put(self, req: pb.DatastorePut) -> pb.GenericResponse:
        ok = await self.runtime_service.datastore_put(req)
        return pb.GenericResponse(status="ok" if ok else "error")

    async def datastore_get(self, req: pb.DatastoreGet) -> pb.DatastoreGetResponse:
        val = await self.runtime_service.datastore_get(req.key)
        return pb.DatastoreGetResponse(value=val or b"")

    async def mailbox_push(self, req: pb.MailboxPush) -> pb.GenericResponse:
        await self.runtime_service.mailbox_push(req.data)
        return pb.GenericResponse(status="ok")

    async def mailbox_read(self, _req: pb.SubscribeRequest) -> pb.MailboxReadResponse:
        val = await self.runtime_service.mailbox_read()
        return pb.MailboxReadResponse(content=val or b"")

    async def file_write(self, req: pb.FileWrite) -> pb.GenericResponse:
        ok = await self.runtime_service.file_write(req.path, req.data)
        return pb.GenericResponse(
            status="ok" if ok else "error", message="" if ok else "Path not allowed or quota exceeded"
        )

    async def file_read(self, req: pb.FileRead) -> pb.FileReadResponse:
        content = await self.runtime_service.file_read(req.path)
        return pb.FileReadResponse(content=content or b"")

    async def file_remove(self, req: pb.FileRemove) -> pb.GenericResponse:
        ok = await self.runtime_service.file_remove(req.path)
        return pb.GenericResponse(status="ok" if ok else "error", message="" if ok else "Path not allowed or not found")

    async def process_run_async(self, req: pb.ProcessRunAsync) -> pb.ProcessRunAsyncResponse:
        pid = await self.runtime_service.run_process(req.command)
        return pb.ProcessRunAsyncResponse(pid=pid or 0)

    async def process_poll(self, req: pb.ProcessPoll) -> pb.ProcessPollResponse:
        return await self.runtime_service.poll_process(req.pid)

    async def process_kill(self, req: pb.ProcessKill) -> pb.GenericResponse:
        ok, err = await self.runtime_service.kill_process(req.pid)
        return (
            pb.GenericResponse(status="ok")
            if ok
            else pb.GenericResponse(status="error", message=err or "PID not found")
        )

    async def spi_configure(self, req: pb.SpiConfig) -> pb.GenericResponse:
        serial = self.runtime_service.serial
        ok = bool(
            serial
            and await serial.send(Command.CMD_SPI_BEGIN.value, b"")
            and await serial.send(Command.CMD_SPI_SET_CONFIG.value, req)
        )
        return pb.GenericResponse(status="ok" if ok else "error")

    async def get_version(self, _req: pb.SubscribeRequest) -> pb.VersionResponse:
        resp = await self._send_serial_query(Command.CMD_GET_VERSION, b"", pb.VersionResponse)
        if resp.major or resp.minor or resp.patch:
            self.runtime_service.state.mcu_version = (resp.major, resp.minor, resp.patch)
        return resp

    async def get_status(self, _req: pb.SubscribeRequest) -> pb.BridgeStatus:
        return self.runtime_service.state.build_status_snapshot()

    async def enter_bootloader(self, req: pb.EnterBootloader) -> pb.GenericResponse:
        serial = self.runtime_service.serial
        magic = req.magic or BOOTLOADER_MAGIC
        ok = bool(serial and await serial.send(Command.CMD_ENTER_BOOTLOADER.value, pb.EnterBootloader(magic=magic)))
        return pb.GenericResponse(status="ok" if ok else "error")

    async def console_write(self, req: pb.ConsoleWrite) -> pb.GenericResponse:
        if pl := req.data:
            self.runtime_service.state.console_to_mcu_queue.append(pl)
            await self.runtime_service.flush_console_queue()
        return pb.GenericResponse(status="ok")

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
        if not spec or not spec.request_type:
            raise ValueError(f"Unknown RPC method: {method_name}")
        req = spec.request_type()
        if payload_bytes:
            req.ParseFromString(payload_bytes)

        with structlog.contextvars.bound_contextvars(rpc_method=method_name):
            if spec.handler_name and hasattr(self, spec.handler_name):
                resp = await getattr(self, spec.handler_name)(req)
            elif spec.requires_ack and spec.command_id is not None:
                resp = await self._send_serial_ack(Command(spec.command_id), req)
            elif spec.response_type is not None and spec.command_id is not None:
                payload = b"" if isinstance(req, pb.SubscribeRequest) else req
                resp = await self._send_serial_query(Command(spec.command_id), payload, spec.response_type)
            else:
                raise ValueError(f"No execution path for {method_name}")
            return resp.SerializeToString()

    @staticmethod
    def create_unary_handler(spec: OperationSpec) -> Callable[[Any, Stream[Any, Any]], Awaitable[None]]:
        name = spec.rpc_name or spec.name
        handler_name = spec.handler_name
        cmd_id = spec.command_id
        resp_cls = spec.response_type

        if handler_name and hasattr(LocalBridgeService, handler_name):

            async def custom_unary_handler(self: LocalBridgeService, stream: Stream[Any, Any]) -> None:
                if (req := await stream.recv_message()) is not None:
                    with structlog.contextvars.bound_contextvars(rpc=name):
                        await stream.send_message(await getattr(self, handler_name)(req))

            custom_unary_handler.__name__ = name
            custom_unary_handler.__qualname__ = f"LocalBridgeService.{name}"
            return custom_unary_handler

        if spec.requires_ack and cmd_id is not None:
            cmd = Command(cmd_id)

            async def ack_unary_handler(self: LocalBridgeService, stream: Stream[Any, Any]) -> None:
                if (req := await stream.recv_message()) is not None:
                    with structlog.contextvars.bound_contextvars(rpc=name):
                        await stream.send_message(await self._send_serial_ack(cmd, req))

            ack_unary_handler.__name__ = name
            ack_unary_handler.__qualname__ = f"LocalBridgeService.{name}"
            return ack_unary_handler

        if resp_cls is not None and cmd_id is not None:
            cmd = Command(cmd_id)

            async def query_unary_handler(self: LocalBridgeService, stream: Stream[Any, Any]) -> None:
                if (req := await stream.recv_message()) is not None:
                    with structlog.contextvars.bound_contextvars(rpc=name):
                        payload = b"" if isinstance(req, pb.SubscribeRequest) else req
                        await stream.send_message(await self._send_serial_query(cmd, payload, resp_cls))

            query_unary_handler.__name__ = name
            query_unary_handler.__qualname__ = f"LocalBridgeService.{name}"
            return query_unary_handler

        raise ValueError(f"Cannot create unary handler for {name}")

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


for _spec in REGISTRY.rpc_specs:
    _name = _spec.rpc_name or _spec.name
    if _name not in LocalBridgeService.__dict__:
        setattr(LocalBridgeService, _name, LocalBridgeService.create_unary_handler(_spec))
    if _spec.handler_name and _spec.handler_name not in LocalBridgeService.__dict__:
        setattr(LocalBridgeService, _spec.handler_name, getattr(LocalBridgeService, _name))

LocalBridgeBase.register(LocalBridgeService)
