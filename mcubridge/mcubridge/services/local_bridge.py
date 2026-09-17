"""Local gRPC Service implementation for local MPU clients over UNIX domain socket. [SIL-2]"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Final, TypeVar

from google.protobuf.message import (
    DecodeError as ProtobufDecodeError,
    Message as ProtobufMessage,
)
from grpclib.server import Stream
import structlog

from ..config.const import MCU_FS_PREFIX
from ..protocol import mcubridge_pb2 as pb
from ..protocol.mcubridge_grpc import LocalBridgeBase
from ..protocol.protocol import (
    Command,
    DatastoreAction,
    FileAction,
    PinAction,
    SpiAction,
    Topic,
)
from ..protocol.structures import is_command_allowed
from ..protocol.topics import parse_topic

if TYPE_CHECKING:
    from .runtime import BridgeService

__all__ = [
    "LocalBridgeService",
    "QUERY_TOPIC_ACTIONS",
    "parse_serial_response",
    "_QUERY_TOPIC_ACTIONS",
    "_parse_serial_response",
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
_QUERY_TOPIC_ACTIONS: Final = QUERY_TOPIC_ACTIONS

_T_PB = TypeVar("_T_PB", bound=ProtobufMessage)


def parse_serial_response(res: Any, target_type: type[_T_PB], default: _T_PB) -> _T_PB:
    """Safely decode serial response payload to target Protobuf message type. [SIL-2]"""
    if isinstance(res, target_type):
        return res
    if isinstance(res, (bytes, bytearray)):
        try:
            return target_type.FromString(bytes(res))
        except (ProtobufDecodeError, TypeError, ValueError):
            return default
    return default


_parse_serial_response = parse_serial_response


class LocalBridgeService(LocalBridgeBase):
    """Implementation of the Local gRPC service for local MPU clients."""

    def __init__(self, runtime_service: BridgeService) -> None:
        self.runtime_service = runtime_service

    async def _dispatch_serial_generic(
        self,
        stream: Stream[Any, pb.GenericResponse],
        cmd: Command,
    ) -> None:
        request = await stream.recv_message()
        if request is None:
            return
        serial = self.runtime_service.serial
        res = (await serial.send(cmd.value, request)) if serial else None
        await stream.send_message(pb.GenericResponse(status="ok" if res is not None else "error"))

    async def _dispatch_serial_typed(
        self,
        stream: Stream[Any, Any],
        cmd: Command,
        resp_cls: type[ProtobufMessage],
        default_resp: ProtobufMessage | None = None,
        *,
        payload: Any = None,
    ) -> None:
        request = await stream.recv_message()
        if request is None:
            return
        serial = self.runtime_service.serial
        send_payload = request if payload is None else payload
        res = (await serial.send(cmd.value, send_payload)) if serial else None
        def_val = default_resp if default_resp is not None else resp_cls()
        await stream.send_message(_parse_serial_response(res, resp_cls, def_val))

    async def SetPinMode(self, stream: Stream[pb.PinMode, pb.GenericResponse]) -> None:
        await self._dispatch_serial_generic(stream, Command.CMD_SET_PIN_MODE)

    async def DigitalWrite(self, stream: Stream[pb.DigitalWrite, pb.GenericResponse]) -> None:
        await self._dispatch_serial_generic(stream, Command.CMD_DIGITAL_WRITE)

    async def DigitalRead(self, stream: Stream[pb.PinRead, pb.DigitalReadResponse]) -> None:
        await self._dispatch_serial_typed(stream, Command.CMD_DIGITAL_READ, pb.DigitalReadResponse)

    async def AnalogWrite(self, stream: Stream[pb.AnalogWrite, pb.GenericResponse]) -> None:
        await self._dispatch_serial_generic(stream, Command.CMD_ANALOG_WRITE)

    async def AnalogRead(self, stream: Stream[pb.PinRead, pb.AnalogReadResponse]) -> None:
        await self._dispatch_serial_typed(stream, Command.CMD_ANALOG_READ, pb.AnalogReadResponse)

    async def PinSubscribe(self, stream: Stream[pb.PinSubscribeRequest, pb.PinSubscribeResponse]) -> None:
        request = await stream.recv_message()
        if request is None:
            return
        try:
            mode_str = pb.PinModeType.Name(request.mode).removeprefix("PIN_")
        except (ValueError, KeyError):
            mode_str = "INPUT"
        res = await self.runtime_service.gpio.subscribe_pin(
            pin=request.pin,
            mode=mode_str,
            interval_ms=request.interval_ms,
            hysteresis=request.hysteresis,
            enabled=request.enabled,
        )
        await stream.send_message(
            pb.PinSubscribeResponse(
                pin=request.pin,
                success=bool(res.get("status") == "ok"),
            )
        )

    async def DatastorePut(self, stream: Stream[pb.DatastorePut, pb.GenericResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        if self.runtime_service.state.datastore_cache is not None:
            await self.runtime_service.state.datastore_cache.set(request.key, request.value)
        await self.runtime_service.publish_datastore_value(request.key, request.value)
        await stream.send_message(pb.GenericResponse(status="ok"))

    async def DatastoreGet(self, stream: Stream[pb.DatastoreGet, pb.DatastoreGetResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        cache = self.runtime_service.state.datastore_cache
        val = (await cache.get(request.key, b"")) if cache else b""
        await stream.send_message(pb.DatastoreGetResponse(value=val or b""))

    async def MailboxPush(self, stream: Stream[pb.MailboxPush, pb.GenericResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        await self.runtime_service.state.mailbox_queue.append(request.data)
        await stream.send_message(pb.GenericResponse(status="ok"))

    async def MailboxRead(self, stream: Stream[pb.SubscribeRequest, pb.MailboxReadResponse]) -> None:
        if (await stream.recv_message()) is None:
            return
        q = self.runtime_service.state.mailbox_incoming_queue
        val = await q.popleft() if len(q) > 0 else b""
        await stream.send_message(pb.MailboxReadResponse(content=val or b""))

    async def _dispatch_file_mutation(
        self,
        stream: Stream[Any, pb.GenericResponse],
        request: pb.FileWrite | pb.FileRemove,
        cmd: Command,
        mcu_msg: ProtobufMessage,
        local_op: Callable[[], Coroutine[Any, Any, bool]],
        error_msg: str,
    ) -> None:
        if request.path.startswith(MCU_FS_PREFIX):
            serial = self.runtime_service.serial
            ok = bool(await serial.send(cmd.value, mcu_msg)) if serial else False
            await stream.send_message(pb.GenericResponse(status="ok" if ok else "error"))
            return
        if await local_op():
            await stream.send_message(pb.GenericResponse(status="ok"))
        else:
            await stream.send_message(pb.GenericResponse(status="error", message=error_msg))

    async def FileWrite(self, stream: Stream[pb.FileWrite, pb.GenericResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        await self._dispatch_file_mutation(
            stream,
            request,
            Command.CMD_FILE_WRITE,
            pb.FileWrite(path=request.path.removeprefix(MCU_FS_PREFIX), data=request.data),
            lambda: self.runtime_service.safe_file_write(request.path, request.data),
            "Path not allowed or quota exceeded",
        )

    async def FileRead(self, stream: Stream[pb.FileRead, pb.FileReadResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        if request.path.startswith(MCU_FS_PREFIX):
            serial = self.runtime_service.serial
            res = (
                await serial.send(
                    Command.CMD_FILE_READ.value,
                    pb.FileRead(path=request.path.removeprefix(MCU_FS_PREFIX)),
                )
                if serial
                else None
            )
            await stream.send_message(_parse_serial_response(res, pb.FileReadResponse, pb.FileReadResponse()))
            return
        content = await self.runtime_service.safe_file_read(request.path)
        await stream.send_message(pb.FileReadResponse(content=content or b""))

    async def FileRemove(self, stream: Stream[pb.FileRemove, pb.GenericResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        await self._dispatch_file_mutation(
            stream,
            request,
            Command.CMD_FILE_REMOVE,
            pb.FileRemove(path=request.path.removeprefix(MCU_FS_PREFIX)),
            lambda: self.runtime_service.safe_file_remove(request.path),
            "Path not allowed or not found",
        )

    async def ProcessRunAsync(self, stream: Stream[pb.ProcessRunAsync, pb.ProcessRunAsyncResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        pid = (
            await self.runtime_service.run_process(request.command)
            if request.command and is_command_allowed(self.runtime_service.state.allowed_policy, request.command)
            else 0
        )
        await stream.send_message(pb.ProcessRunAsyncResponse(pid=pid or 0))

    async def ProcessPoll(self, stream: Stream[pb.ProcessPoll, pb.ProcessPollResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        await stream.send_message(await self.runtime_service.poll_process(request.pid))

    async def ProcessKill(self, stream: Stream[pb.ProcessKill, pb.GenericResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        ok, err = await self.runtime_service.kill_process(request.pid)
        if ok:
            await stream.send_message(pb.GenericResponse(status="ok"))
        else:
            await stream.send_message(pb.GenericResponse(status="error", message=err or "PID not found"))

    async def SpiTransfer(self, stream: Stream[pb.SpiTransfer, pb.SpiTransferResponse]) -> None:
        await self._dispatch_serial_typed(stream, Command.CMD_SPI_TRANSFER, pb.SpiTransferResponse)

    async def SpiConfigure(self, stream: Stream[pb.SpiConfig, pb.GenericResponse]) -> None:
        if (request := await stream.recv_message()) is None:
            return
        serial = self.runtime_service.serial
        ok = False
        if serial:
            await serial.send(Command.CMD_SPI_BEGIN.value, b"")
            ok = bool(await serial.send(Command.CMD_SPI_SET_CONFIG.value, request))
        await stream.send_message(pb.GenericResponse(status="ok" if ok else "error"))

    async def GetVersion(self, stream: Stream[pb.SubscribeRequest, pb.VersionResponse]) -> None:
        await self._dispatch_serial_typed(stream, Command.CMD_GET_VERSION, pb.VersionResponse, payload=b"")

    async def GetFreeMemory(self, stream: Stream[pb.SubscribeRequest, pb.FreeMemoryResponse]) -> None:
        await self._dispatch_serial_typed(stream, Command.CMD_GET_FREE_MEMORY, pb.FreeMemoryResponse, payload=b"")

    async def GetStatus(self, stream: Stream[pb.SubscribeRequest, pb.BridgeStatus]) -> None:
        request = await stream.recv_message()
        if request is None:
            return
        status = self.runtime_service.state.build_status_snapshot()
        await stream.send_message(status)

    async def Publish(self, stream: Stream[pb.CloudQueuedPublish, pb.CloudQueuedPublish]) -> None:
        request = await stream.recv_message()
        if request is None:
            return

        has_correlation = request.HasField("correlation_data")
        route = parse_topic(self.runtime_service.state.cloud_topic_prefix, request.topic_name)
        action = self.runtime_service.deduce_action(route) if route else None

        is_query = has_correlation or (
            route is not None
            and (
                (route.topic, action) in _QUERY_TOPIC_ACTIONS
                or (
                    route.topic == Topic.SYSTEM
                    and ("get" in route.segments or action in ("version", "freeram", "bridge"))
                )
            )
        )

        correlation = request.correlation_data if has_correlation else (secrets.token_bytes(12) if is_query else b"")

        with structlog.contextvars.bound_contextvars(
            topic=request.topic_name,
            correlation=correlation.hex() if correlation else "",
        ):
            response_queue: asyncio.Queue[pb.CloudQueuedPublish] | None = None
            if is_query and correlation:
                response_queue = asyncio.Queue(maxsize=1)
                self.runtime_service.ipc_requests[correlation] = response_queue
                logger.debug("Registering IPC request correlation")

            try:
                req = pb.CloudQueuedPublish(
                    topic_name=request.topic_name,
                    payload=request.payload,
                    correlation_data=correlation,
                )

                await self.runtime_service.handle_request(req)

                if is_query and response_queue is not None:
                    try:
                        async with asyncio.timeout(15.0):
                            response = await response_queue.get()
                            await stream.send_message(response)
                    except TimeoutError:
                        logger.warning("IPC request timed out")
                        await stream.send_message(pb.CloudQueuedPublish())
                else:
                    await stream.send_message(pb.CloudQueuedPublish())
            except OSError as exc:
                logger.debug("IPC connection closed during response write", error=str(exc))
            finally:
                if is_query and correlation:
                    self.runtime_service.ipc_requests.pop(correlation, None)

    async def SubscribeConsole(self, stream: Stream[pb.SubscribeRequest, pb.CloudQueuedPublish]) -> None:
        request = await stream.recv_message()
        if request is None:
            return

        queue: asyncio.Queue[pb.CloudQueuedPublish] = asyncio.Queue()
        self.runtime_service.console_queues.append(queue)
        try:
            while True:
                msg = await queue.get()
                await stream.send_message(msg)
        except (OSError, RuntimeError) as e:
            logger.error("Local IPC console stream error", error=str(e))
            raise
        finally:
            if queue in self.runtime_service.console_queues:
                self.runtime_service.console_queues.remove(queue)
