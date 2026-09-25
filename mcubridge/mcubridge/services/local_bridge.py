"""Local gRPC Service implementation for local MPU clients over UNIX domain socket. [SIL-2]"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Final

from google.protobuf.message import DecodeError as ProtobufDecodeError, Message as ProtobufMessage
from grpclib.server import Stream
import structlog
import structlog.contextvars

from ..config.const import MCU_FS_PREFIX
from ..protocol import mcubridge_pb2 as pb
from ..protocol.mcubridge_grpc import LocalBridgeBase
from ..protocol.protocol import AEAD_NONCE_SIZE, Command, DatastoreAction, FileAction, PinAction, SpiAction, Topic
from ..protocol.structures import is_command_allowed
from ..protocol.topics import parse_topic

if TYPE_CHECKING:
    from .runtime import BridgeService

__all__ = [
    "LocalBridgeService",
    "QUERY_TOPIC_ACTIONS",
    "RPC_DISPATCH_TABLE",
    "parse_serial_response",
]

logger = structlog.get_logger("mcubridge.service.local_bridge")

QUERY_TOPIC_ACTIONS: Final = frozenset(
    {
        (Topic.DIGITAL, PinAction.READ),
        (Topic.ANALOG, PinAction.READ),
        (Topic.DATASTORE, DatastoreAction.GET),
        (Topic.FILE, FileAction.READ),
        (Topic.SPI, SpiAction.TRANSFER),
    }
)


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


class LocalBridgeService(LocalBridgeBase):
    """Implementation of the Local gRPC service for local MPU clients and Cloud Gateway forwarding."""

    def __init__(self, runtime_service: BridgeService) -> None:
        self.runtime_service = runtime_service

    # --- Generic Unary Dispatcher (Zero-Boilerplate & Contextvars Propagation) ---

    async def _handle_unary(
        self,
        stream: Stream[Any, Any],
        handler: Callable[[LocalBridgeService, Any], Coroutine[Any, Any, Any]],
        rpc_name: str,
    ) -> None:
        if (req := await stream.recv_message()) is not None:
            with structlog.contextvars.bound_contextvars(rpc=rpc_name):
                await stream.send_message(await handler(self, req))

    # --- Core Business Logic Execution (Zero-Duplication) ---

    async def execute_set_pin_mode(self, req: pb.PinMode) -> pb.GenericResponse:
        with structlog.contextvars.bound_contextvars(pin=req.pin, mode=req.mode):
            serial = self.runtime_service.serial
            res = (await serial.send(Command.CMD_SET_PIN_MODE.value, req)) if serial else None
            return pb.GenericResponse(status="ok" if res is not None else "error")

    async def execute_digital_write(self, req: pb.DigitalWrite) -> pb.GenericResponse:
        with structlog.contextvars.bound_contextvars(pin=req.pin, value=req.value):
            serial = self.runtime_service.serial
            res = (await serial.send(Command.CMD_DIGITAL_WRITE.value, req)) if serial else None
            return pb.GenericResponse(status="ok" if res is not None else "error")

    async def execute_digital_read(self, req: pb.PinRead) -> pb.DigitalReadResponse:
        with structlog.contextvars.bound_contextvars(pin=req.pin):
            serial = self.runtime_service.serial
            res = (await serial.send(Command.CMD_DIGITAL_READ.value, req)) if serial else None
            return parse_serial_response(res, pb.DigitalReadResponse, pb.DigitalReadResponse())

    async def execute_analog_write(self, req: pb.AnalogWrite) -> pb.GenericResponse:
        with structlog.contextvars.bound_contextvars(pin=req.pin, value=req.value):
            serial = self.runtime_service.serial
            res = (await serial.send(Command.CMD_ANALOG_WRITE.value, req)) if serial else None
            return pb.GenericResponse(status="ok" if res is not None else "error")

    async def execute_analog_read(self, req: pb.PinRead) -> pb.AnalogReadResponse:
        with structlog.contextvars.bound_contextvars(pin=req.pin):
            serial = self.runtime_service.serial
            res = (await serial.send(Command.CMD_ANALOG_READ.value, req)) if serial else None
            return parse_serial_response(res, pb.AnalogReadResponse, pb.AnalogReadResponse())

    async def execute_pin_subscribe(self, req: pb.PinSubscribeRequest) -> pb.PinSubscribeResponse:
        with structlog.contextvars.bound_contextvars(pin=req.pin, enabled=req.enabled):
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
        with structlog.contextvars.bound_contextvars(key=req.key):
            if self.runtime_service.state.datastore_cache is not None:
                await self.runtime_service.state.datastore_cache.set(req.key, req.value)
            await self.runtime_service.publish_datastore_value(req.key, req.value)
            return pb.GenericResponse(status="ok")

    async def execute_datastore_get(self, req: pb.DatastoreGet) -> pb.DatastoreGetResponse:
        with structlog.contextvars.bound_contextvars(key=req.key):
            c = self.runtime_service.state.datastore_cache
            val = (await c.get(req.key, b"")) if c else b""
            return pb.DatastoreGetResponse(value=val or b"")

    async def execute_mailbox_push(self, req: pb.MailboxPush) -> pb.GenericResponse:
        with structlog.contextvars.bound_contextvars(mailbox_op="push"):
            await self.runtime_service.state.mailbox_queue.append(req.data)
            return pb.GenericResponse(status="ok")

    async def execute_mailbox_read(self, _req: pb.SubscribeRequest) -> pb.MailboxReadResponse:
        with structlog.contextvars.bound_contextvars(mailbox_op="read"):
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
        with structlog.contextvars.bound_contextvars(file_cmd=cmd.name, path=request.path):
            if request.path.startswith(MCU_FS_PREFIX):
                serial = self.runtime_service.serial
                ok = bool(await serial.send(cmd.value, mcu_msg)) if serial else False
                return pb.GenericResponse(status="ok" if ok else "error")
            if await local_op():
                return pb.GenericResponse(status="ok")
            return pb.GenericResponse(status="error", message=error_msg)

    async def execute_file_write(self, req: pb.FileWrite) -> pb.GenericResponse:
        return await self._execute_file_mutation(
            req,
            Command.CMD_FILE_WRITE,
            pb.FileWrite(path=req.path.removeprefix(MCU_FS_PREFIX), data=req.data),
            lambda: self.runtime_service.safe_file_write(req.path, req.data),
            "Path not allowed or quota exceeded",
        )

    async def execute_file_read(self, req: pb.FileRead) -> pb.FileReadResponse:
        with structlog.contextvars.bound_contextvars(path=req.path):
            if req.path.startswith(MCU_FS_PREFIX):
                serial = self.runtime_service.serial
                clean_path = req.path.removeprefix(MCU_FS_PREFIX)
                res = (await serial.send(Command.CMD_FILE_READ.value, pb.FileRead(path=clean_path))) if serial else None
                return parse_serial_response(res, pb.FileReadResponse, pb.FileReadResponse())
            content = await self.runtime_service.safe_file_read(req.path)
            return pb.FileReadResponse(content=content or b"")

    async def execute_file_remove(self, req: pb.FileRemove) -> pb.GenericResponse:
        return await self._execute_file_mutation(
            req,
            Command.CMD_FILE_REMOVE,
            pb.FileRemove(path=req.path.removeprefix(MCU_FS_PREFIX)),
            lambda: self.runtime_service.safe_file_remove(req.path),
            "Path not allowed or not found",
        )

    async def execute_process_run_async(self, req: pb.ProcessRunAsync) -> pb.ProcessRunAsyncResponse:
        with structlog.contextvars.bound_contextvars(command=req.command):
            allowed = is_command_allowed(self.runtime_service.state.allowed_policy, req.command)
            pid = (await self.runtime_service.run_process(req.command)) if (req.command and allowed) else 0
            return pb.ProcessRunAsyncResponse(pid=pid or 0)

    async def execute_process_poll(self, req: pb.ProcessPoll) -> pb.ProcessPollResponse:
        with structlog.contextvars.bound_contextvars(pid=req.pid):
            return await self.runtime_service.poll_process(req.pid)

    async def execute_process_kill(self, req: pb.ProcessKill) -> pb.GenericResponse:
        with structlog.contextvars.bound_contextvars(pid=req.pid):
            ok, err = await self.runtime_service.kill_process(req.pid)
            return (
                pb.GenericResponse(status="ok")
                if ok
                else pb.GenericResponse(status="error", message=err or "PID not found")
            )

    async def execute_spi_transfer(self, req: pb.SpiTransfer) -> pb.SpiTransferResponse:
        serial = self.runtime_service.serial
        res = (await serial.send(Command.CMD_SPI_TRANSFER.value, req)) if serial else None
        return parse_serial_response(res, pb.SpiTransferResponse, pb.SpiTransferResponse())

    async def execute_spi_configure(self, req: pb.SpiConfig) -> pb.GenericResponse:
        serial = self.runtime_service.serial
        ok = bool(
            serial
            and await serial.send(Command.CMD_SPI_BEGIN.value, b"")
            and await serial.send(Command.CMD_SPI_SET_CONFIG.value, req)
        )
        return pb.GenericResponse(status="ok" if ok else "error")

    async def execute_get_version(self, _req: pb.SubscribeRequest) -> pb.VersionResponse:
        serial = self.runtime_service.serial
        res = (await serial.send(Command.CMD_GET_VERSION.value, b"")) if serial else None
        return parse_serial_response(res, pb.VersionResponse, pb.VersionResponse())

    async def execute_get_free_memory(self, _req: pb.SubscribeRequest) -> pb.FreeMemoryResponse:
        serial = self.runtime_service.serial
        res = (await serial.send(Command.CMD_GET_FREE_MEMORY.value, b"")) if serial else None
        return parse_serial_response(res, pb.FreeMemoryResponse, pb.FreeMemoryResponse())

    async def execute_get_status(self, _req: pb.SubscribeRequest) -> pb.BridgeStatus:
        return self.runtime_service.state.build_status_snapshot()

    async def execute_publish(self, req: pb.CloudQueuedPublish) -> pb.CloudQueuedPublish:
        has_correlation = req.HasField("correlation_data")
        route = parse_topic(self.runtime_service.state.cloud_topic_prefix, req.topic_name)
        action = self.runtime_service.deduce_action(route) if route else None

        is_query = has_correlation or (
            route is not None
            and (
                (route.topic, action) in QUERY_TOPIC_ACTIONS
                or (
                    route.topic == Topic.SYSTEM
                    and ("get" in route.segments or action in ("version", "freeram", "bridge"))
                )
            )
        )

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
                        async with asyncio.timeout(15.0):
                            return await response_queue.get()
                    except TimeoutError:
                        logger.warning("IPC request timed out")
                        return pb.CloudQueuedPublish()
                return pb.CloudQueuedPublish()
            finally:
                if is_query and correlation:
                    self.runtime_service.ipc_requests.pop(correlation, None)

    async def execute_rpc(self, method_name: str, payload_bytes: bytes) -> bytes:
        """[SIL-2] Dispatch and execute an RPC by name, returning serialized response."""
        handler_entry = RPC_DISPATCH_TABLE.get(method_name)
        if not handler_entry:
            raise ValueError(f"Unknown RPC method: {method_name}")
        req_cls, handler = handler_entry
        req = req_cls()
        if payload_bytes:
            req.ParseFromString(payload_bytes)
        with structlog.contextvars.bound_contextvars(rpc_method=method_name):
            resp = await handler(self, req)
            return resp.SerializeToString()

    # --- LocalBridgeBase Stream Handlers (Delegating via _handle_unary) ---

    async def SetPinMode(self, stream: Stream[pb.PinMode, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_set_pin_mode, "SetPinMode")

    async def DigitalWrite(self, stream: Stream[pb.DigitalWrite, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_digital_write, "DigitalWrite")

    async def DigitalRead(self, stream: Stream[pb.PinRead, pb.DigitalReadResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_digital_read, "DigitalRead")

    async def AnalogWrite(self, stream: Stream[pb.AnalogWrite, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_analog_write, "AnalogWrite")

    async def AnalogRead(self, stream: Stream[pb.PinRead, pb.AnalogReadResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_analog_read, "AnalogRead")

    async def PinSubscribe(self, stream: Stream[pb.PinSubscribeRequest, pb.PinSubscribeResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_pin_subscribe, "PinSubscribe")

    async def DatastorePut(self, stream: Stream[pb.DatastorePut, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_datastore_put, "DatastorePut")

    async def DatastoreGet(self, stream: Stream[pb.DatastoreGet, pb.DatastoreGetResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_datastore_get, "DatastoreGet")

    async def MailboxPush(self, stream: Stream[pb.MailboxPush, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_mailbox_push, "MailboxPush")

    async def MailboxRead(self, stream: Stream[pb.SubscribeRequest, pb.MailboxReadResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_mailbox_read, "MailboxRead")

    async def FileWrite(self, stream: Stream[pb.FileWrite, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_file_write, "FileWrite")

    async def FileRead(self, stream: Stream[pb.FileRead, pb.FileReadResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_file_read, "FileRead")

    async def FileRemove(self, stream: Stream[pb.FileRemove, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_file_remove, "FileRemove")

    async def ProcessRunAsync(self, stream: Stream[pb.ProcessRunAsync, pb.ProcessRunAsyncResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_process_run_async, "ProcessRunAsync")

    async def ProcessPoll(self, stream: Stream[pb.ProcessPoll, pb.ProcessPollResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_process_poll, "ProcessPoll")

    async def ProcessKill(self, stream: Stream[pb.ProcessKill, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_process_kill, "ProcessKill")

    async def SpiTransfer(self, stream: Stream[pb.SpiTransfer, pb.SpiTransferResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_spi_transfer, "SpiTransfer")

    async def SpiConfigure(self, stream: Stream[pb.SpiConfig, pb.GenericResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_spi_configure, "SpiConfigure")

    async def GetVersion(self, stream: Stream[pb.SubscribeRequest, pb.VersionResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_get_version, "GetVersion")

    async def GetFreeMemory(self, stream: Stream[pb.SubscribeRequest, pb.FreeMemoryResponse]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_get_free_memory, "GetFreeMemory")

    async def GetStatus(self, stream: Stream[pb.SubscribeRequest, pb.BridgeStatus]) -> None:
        await self._handle_unary(stream, LocalBridgeService.execute_get_status, "GetStatus")

    async def Publish(self, stream: Stream[pb.CloudQueuedPublish, pb.CloudQueuedPublish]) -> None:
        if (req := await stream.recv_message()) is not None:
            try:
                await stream.send_message(await self.execute_publish(req))
            except OSError as exc:
                logger.debug("IPC connection closed during response write", error=str(exc))

    async def SubscribeConsole(self, stream: Stream[pb.SubscribeRequest, pb.CloudQueuedPublish]) -> None:
        if await stream.recv_message() is None:
            return

        queue: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
        self.runtime_service.console_queues.append(queue)
        try:
            while True:
                await stream.send_message(await queue.get())
        except (OSError, RuntimeError) as e:
            logger.error("Local IPC console stream error", error=str(e))
            raise
        finally:
            if queue in self.runtime_service.console_queues:
                self.runtime_service.console_queues.remove(queue)


RPC_DISPATCH_TABLE: Final[
    dict[
        str,
        tuple[
            type[ProtobufMessage],
            Callable[[LocalBridgeService, Any], Coroutine[Any, Any, ProtobufMessage]],
        ],
    ]
] = {
    "SetPinMode": (pb.PinMode, LocalBridgeService.execute_set_pin_mode),
    "DigitalWrite": (pb.DigitalWrite, LocalBridgeService.execute_digital_write),
    "DigitalRead": (pb.PinRead, LocalBridgeService.execute_digital_read),
    "AnalogWrite": (pb.AnalogWrite, LocalBridgeService.execute_analog_write),
    "AnalogRead": (pb.PinRead, LocalBridgeService.execute_analog_read),
    "PinSubscribe": (pb.PinSubscribeRequest, LocalBridgeService.execute_pin_subscribe),
    "DatastorePut": (pb.DatastorePut, LocalBridgeService.execute_datastore_put),
    "DatastoreGet": (pb.DatastoreGet, LocalBridgeService.execute_datastore_get),
    "MailboxPush": (pb.MailboxPush, LocalBridgeService.execute_mailbox_push),
    "MailboxRead": (pb.SubscribeRequest, LocalBridgeService.execute_mailbox_read),
    "FileWrite": (pb.FileWrite, LocalBridgeService.execute_file_write),
    "FileRead": (pb.FileRead, LocalBridgeService.execute_file_read),
    "FileRemove": (pb.FileRemove, LocalBridgeService.execute_file_remove),
    "ProcessRunAsync": (pb.ProcessRunAsync, LocalBridgeService.execute_process_run_async),
    "ProcessPoll": (pb.ProcessPoll, LocalBridgeService.execute_process_poll),
    "ProcessKill": (pb.ProcessKill, LocalBridgeService.execute_process_kill),
    "SpiTransfer": (pb.SpiTransfer, LocalBridgeService.execute_spi_transfer),
    "SpiConfigure": (pb.SpiConfig, LocalBridgeService.execute_spi_configure),
    "GetVersion": (pb.SubscribeRequest, LocalBridgeService.execute_get_version),
    "GetFreeMemory": (pb.SubscribeRequest, LocalBridgeService.execute_get_free_memory),
    "GetStatus": (pb.SubscribeRequest, LocalBridgeService.execute_get_status),
    "Publish": (pb.CloudQueuedPublish, LocalBridgeService.execute_publish),
}
