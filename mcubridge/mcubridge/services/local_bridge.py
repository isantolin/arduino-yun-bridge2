"""Local gRPC Service implementation for local MPU clients over UNIX domain socket. [SIL-2]"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Final

import structlog
import structlog.contextvars
from google.protobuf.message import DecodeError as ProtobufDecodeError
from google.protobuf.message import Message as ProtobufMessage
from grpclib.server import Stream

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

    # --- Core Business Logic Execution (Zero-Duplication) ---

    async def execute_set_pin_mode(self, req: pb.PinMode) -> pb.GenericResponse:
        serial = self.runtime_service.serial
        res = (await serial.send(Command.CMD_SET_PIN_MODE.value, req)) if serial else None
        return pb.GenericResponse(status="ok" if res is not None else "error")

    async def execute_digital_write(self, req: pb.DigitalWrite) -> pb.GenericResponse:
        serial = self.runtime_service.serial
        res = (await serial.send(Command.CMD_DIGITAL_WRITE.value, req)) if serial else None
        return pb.GenericResponse(status="ok" if res is not None else "error")

    async def execute_digital_read(self, req: pb.PinRead) -> pb.DigitalReadResponse:
        serial = self.runtime_service.serial
        res = (await serial.send(Command.CMD_DIGITAL_READ.value, req)) if serial else None
        return parse_serial_response(res, pb.DigitalReadResponse, pb.DigitalReadResponse())

    async def execute_analog_write(self, req: pb.AnalogWrite) -> pb.GenericResponse:
        serial = self.runtime_service.serial
        res = (await serial.send(Command.CMD_ANALOG_WRITE.value, req)) if serial else None
        return pb.GenericResponse(status="ok" if res is not None else "error")

    async def execute_analog_read(self, req: pb.PinRead) -> pb.AnalogReadResponse:
        serial = self.runtime_service.serial
        res = (await serial.send(Command.CMD_ANALOG_READ.value, req)) if serial else None
        return parse_serial_response(res, pb.AnalogReadResponse, pb.AnalogReadResponse())

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
        if self.runtime_service.state.datastore_cache is not None:
            await self.runtime_service.state.datastore_cache.set(req.key, req.value)
        await self.runtime_service.publish_datastore_value(req.key, req.value)
        return pb.GenericResponse(status="ok")

    async def execute_datastore_get(self, req: pb.DatastoreGet) -> pb.DatastoreGetResponse:
        c = self.runtime_service.state.datastore_cache
        val = (await c.get(req.key, b"")) if c else b""
        return pb.DatastoreGetResponse(value=val or b"")

    async def execute_mailbox_push(self, req: pb.MailboxPush) -> pb.GenericResponse:
        await self.runtime_service.state.mailbox_queue.append(req.data)
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
