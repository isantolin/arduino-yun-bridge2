"""Direct Pigweed RPC Gateway Client for mcubridge. [SIL-2]"""

from __future__ import annotations

import asyncio
import socket
import ssl
import threading
from typing import Any, Final, cast

import pw_hdlc.decode
import pw_hdlc.encode
import structlog
from pw_rpc.callback_client.impl import Impl as PwCallbackClientImpl
from pw_rpc.client import Client as PwRpcClient
from pw_rpc.descriptors import Channel as PwRpcChannel, Service as PwRpcService, Services as PwRpcServices

from . import mcu_wire_pb2
from .gateway import gateway_control_pb2

PW_SERVICES: Final[PwRpcServices] = PwRpcServices(
    PwRpcService.from_descriptor(desc)
    for mod in (mcu_wire_pb2, gateway_control_pb2)
    for desc in cast(Any, mod).DESCRIPTOR.services_by_name.values()
)

logger = structlog.get_logger(__name__)


class GatewayConnection:
    """Manages an HDLC-framed Pigweed RPC transport over an mTLS or TCP socket."""

    def __init__(
        self,
        host: str,
        port: int,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = 10.0,
    ) -> None:
        raw_sock = socket.create_connection((host, port), timeout=timeout)
        if ssl_context is not None:
            self.sock = ssl_context.wrap_socket(raw_sock, server_hostname="localhost")
        else:
            self.sock = raw_sock

        self.running = True
        self.decoder = pw_hdlc.decode.FrameDecoder()
        self.rpc_client = PwRpcClient(
            PwCallbackClientImpl(),
            [PwRpcChannel(1, self.send_frame)],
            PW_SERVICES,
        )
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def send_frame(self, data: bytes) -> None:
        if self.sock:
            self.sock.sendall(pw_hdlc.encode.ui_frame(1, data))

    def _reader_loop(self) -> None:
        while self.running:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                for frame in self.decoder.process(chunk):
                    if frame.ok() and frame.address == 1:
                        self.rpc_client.process_packet(frame.data)
            except (OSError, ValueError) as exc:
                logger.debug("Reader loop socket error", error=str(exc))
                break

    def close(self) -> None:
        self.running = False
        try:
            self.sock.close()
        except OSError as exc:
            logger.debug("Socket close error ignored", error=str(exc))
        self.reader_thread.join(timeout=2)


class GatewayClient:
    """Direct client communicating with the C++ Gateway via Pigweed GatewayControl."""

    def __init__(self, connection: GatewayConnection, target_device_id: str) -> None:
        self.connection = connection
        self.target_device_id = target_device_id

    async def dispatch(
        self,
        service_name: str,
        method_name: str,
        request: Any = None,
        timeout_ms: int = 5000,
    ) -> bytes:
        payload = request.SerializeToString() if request is not None else b""
        dispatch_method = self.connection.rpc_client.channel(1).rpcs.mcubridge.gateway.GatewayControl.Dispatch
        req = gateway_control_pb2.DispatchRequest(
            target_device_id=self.target_device_id,
            service_name=service_name,
            method_name=method_name,
            request_payload=payload,
            timeout_ms=timeout_ms,
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()

        def _on_completed(call: Any, resp: Any) -> None:
            if not fut.done():
                fut.set_result((call.status, resp))

        def _on_error(call: Any) -> None:
            if not fut.done():
                fut.set_result((call.status, None))

        dispatch_method.invoke(req, on_completed=_on_completed, on_error=_on_error)
        status, resp = await asyncio.wait_for(fut, timeout=max(float(timeout_ms) / 1000.0, 1.0) + 2.0)
        if not status.ok():
            raise RuntimeError(f"Pigweed RPC dispatch failed: {status}")
        return resp.response_payload

    async def Health(self) -> gateway_control_pb2.HealthResponse:
        health_method = self.connection.rpc_client.channel(1).rpcs.mcubridge.gateway.GatewayControl.Health
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()

        def _on_completed(call: Any, resp: Any) -> None:
            if not fut.done():
                fut.set_result((call.status, resp))

        def _on_error(call: Any) -> None:
            if not fut.done():
                fut.set_result((call.status, None))

        health_method.invoke(on_completed=_on_completed, on_error=_on_error)
        status, resp = await asyncio.wait_for(fut, timeout=5.0)
        if not status.ok():
            raise RuntimeError(f"Health check failed: {status}")
        return resp

    async def DigitalWrite(self, req: mcu_wire_pb2.DigitalWrite) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.GpioService", "DigitalWrite", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def DigitalRead(self, req: mcu_wire_pb2.PinRead) -> mcu_wire_pb2.DigitalReadResponse:
        resp_bytes = await self.dispatch("rpc.pb.GpioService", "DigitalRead", req)
        resp = mcu_wire_pb2.DigitalReadResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def AnalogWrite(self, req: mcu_wire_pb2.AnalogWrite) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.GpioService", "AnalogWrite", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def AnalogRead(self, req: mcu_wire_pb2.PinRead) -> mcu_wire_pb2.AnalogReadResponse:
        resp_bytes = await self.dispatch("rpc.pb.GpioService", "AnalogRead", req)
        resp = mcu_wire_pb2.AnalogReadResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def PinSubscribe(self, req: mcu_wire_pb2.PinSubscribeRequest) -> mcu_wire_pb2.PinSubscribeResponse:
        resp_bytes = await self.dispatch("rpc.pb.GpioService", "SubscribePins", req)
        resp = mcu_wire_pb2.PinSubscribeResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def SetPinMode(self, req: mcu_wire_pb2.PinMode) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.GpioService", "SetPinMode", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def ConsoleWrite(self, req: mcu_wire_pb2.ConsoleWrite) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.ConsoleService", "Write", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def DatastorePut(self, req: mcu_wire_pb2.DatastorePut) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.DataStoreService", "Put", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def DatastoreGet(self, req: mcu_wire_pb2.DatastoreGet) -> mcu_wire_pb2.DatastoreGetResponse:
        resp_bytes = await self.dispatch("rpc.pb.DataStoreService", "Get", req)
        resp = mcu_wire_pb2.DatastoreGetResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def MailboxPush(self, req: mcu_wire_pb2.MailboxPush) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.MailboxService", "Push", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def MailboxRead(self, req: mcu_wire_pb2.SubscribeRequest) -> mcu_wire_pb2.MailboxReadResponse:
        resp_bytes = await self.dispatch("rpc.pb.MailboxService", "Read", req)
        resp = mcu_wire_pb2.MailboxReadResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def ProcessRunAsync(self, req: mcu_wire_pb2.ProcessRunAsync) -> mcu_wire_pb2.ProcessRunAsyncResponse:
        resp_bytes = await self.dispatch("rpc.pb.ProcessService", "RunAsync", req)
        resp = mcu_wire_pb2.ProcessRunAsyncResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def ProcessPoll(self, req: mcu_wire_pb2.ProcessPoll) -> mcu_wire_pb2.ProcessPollResponse:
        resp_bytes = await self.dispatch("rpc.pb.ProcessService", "Poll", req)
        resp = mcu_wire_pb2.ProcessPollResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def ProcessKill(self, req: mcu_wire_pb2.ProcessKill) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.ProcessService", "Kill", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def FileWrite(self, req: mcu_wire_pb2.FileWrite) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.FileSystemService", "Write", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def FileRemove(self, req: mcu_wire_pb2.FileRemove) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.FileSystemService", "Remove", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def SpiConfigure(self, req: mcu_wire_pb2.SpiConfig) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.SpiService", "SetConfig", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp

    async def SpiTransfer(self, req: mcu_wire_pb2.SpiTransfer) -> mcu_wire_pb2.SpiTransferResponse:
        resp_bytes = await self.dispatch("rpc.pb.SpiService", "Transfer", req)
        resp = mcu_wire_pb2.SpiTransferResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def GetVersion(self, req: mcu_wire_pb2.SubscribeRequest) -> mcu_wire_pb2.VersionResponse:
        resp_bytes = await self.dispatch("rpc.pb.SystemService", "GetVersion", req)
        resp = mcu_wire_pb2.VersionResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def GetFreeMemory(self, req: mcu_wire_pb2.SubscribeRequest) -> mcu_wire_pb2.FreeMemoryResponse:
        resp_bytes = await self.dispatch("rpc.pb.SystemService", "GetFreeMemory", req)
        resp = mcu_wire_pb2.FreeMemoryResponse()
        resp.ParseFromString(resp_bytes)
        return resp

    async def EnterBootloader(self, req: mcu_wire_pb2.EnterBootloader) -> mcu_wire_pb2.Empty:
        resp_bytes = await self.dispatch("rpc.pb.SystemService", "EnterBootloader", req)
        resp = mcu_wire_pb2.Empty()
        resp.ParseFromString(resp_bytes)
        return resp
