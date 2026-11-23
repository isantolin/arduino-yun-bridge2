"""Direct Pigweed RPC Gateway Client for mcubridge. [SIL-2]"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import select
import socket
import ssl
import threading
import time
from typing import Any, Final, cast

import pw_hdlc.decode
import pw_hdlc.encode
import structlog
from google.protobuf.message import Message as ProtobufMessage
from pw_rpc.callback_client.call import UnaryCall
from pw_rpc.callback_client.impl import Impl as PwCallbackClientImpl
from pw_rpc.client import Client as PwRpcClient
from pw_rpc.descriptors import Channel as PwRpcChannel, Service as PwRpcService, Services as PwRpcServices
from pw_status import Status as PwStatus
from pw_transfer import transfer_pb2
from pw_transfer.client import Manager as PwTransferManager

from . import cloud_gateway_pb2, mcu_wire_pb2, protocol
from .gateway import gateway_control_pb2

PW_SERVICES: Final[PwRpcServices] = PwRpcServices(
    PwRpcService.from_descriptor(desc)
    for mod in (mcu_wire_pb2, cloud_gateway_pb2, gateway_control_pb2, transfer_pb2)
    for desc in cast(Any, mod).DESCRIPTOR.services_by_name.values()
)

# Canonical operation names ("<proto service>/<method>"), the single identifier
# format used end to end by the client, the Gateway and the daemon catalog.
OPERATIONS: Final[frozenset[str]] = frozenset(
    f"{service.full_name}/{method.name}" for service in PW_SERVICES for method in service.methods
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
        logger.info("Gateway connection: opening TCP socket", host=host, port=port, timeout_seconds=timeout)
        raw_sock = socket.create_connection((host, port), timeout=timeout)
        logger.info("Gateway connection: TCP established", host=host, port=port)
        self.sock: socket.socket | ssl.SSLSocket
        if ssl_context is not None:
            logger.info("Gateway connection: starting TLS handshake", tls_minimum=str(ssl_context.minimum_version))
            try:
                self.sock = ssl_context.wrap_socket(raw_sock, server_hostname="localhost")
            except BaseException:
                raw_sock.close()
                raise
            logger.info("Gateway connection: TLS handshake completed", tls_version=self.sock.version())
        else:
            self.sock = raw_sock
            logger.info("Gateway connection: TLS disabled")

        # Non-blocking I/O with select() outside the lock lets reader and writer
        # share a single ssl.SSLSocket safely without concurrent OpenSSL calls.
        self.sock.setblocking(False)

        self.running = True
        self._closed = False
        self._io_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.decoder = pw_hdlc.decode.FrameDecoder()
        self.rpc_client = PwRpcClient(
            PwCallbackClientImpl(),
            [PwRpcChannel(1, self.send_frame)],
            PW_SERVICES,
        )
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True, name="mcubridge-gateway-reader")
        self.reader_thread.start()
        logger.info("Gateway connection: RPC reader thread started")

    def send_frame(self, data: bytes) -> None:
        frame = pw_hdlc.encode.ui_frame(1, data)
        view = memoryview(frame)
        deadline = time.monotonic() + 10.0
        with self._send_lock:
            while view:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out sending Gateway HDLC frame")
                wait_lists: tuple[list[socket.socket], list[socket.socket]] | None = None
                with self._io_lock:
                    if self._closed:
                        return
                    try:
                        sent = self.sock.send(view)
                    except ssl.SSLWantReadError:
                        wait_lists = ([self.sock], [])
                    except (ssl.SSLWantWriteError, BlockingIOError):
                        wait_lists = ([], [self.sock])
                    else:
                        if sent == 0:
                            raise ConnectionError("Gateway socket closed during send")
                        view = view[sent:]
                if wait_lists is not None:
                    try:
                        select.select(wait_lists[0], wait_lists[1], [], 0.05)
                    except (OSError, ValueError):
                        return

    def _reader_loop(self) -> None:
        while self.running:
            with self._io_lock:
                has_pending = isinstance(self.sock, ssl.SSLSocket) and self.sock.pending() > 0
            if not has_pending:
                try:
                    readable, _, _ = select.select([self.sock], [], [], 0.05)
                except (OSError, ValueError) as exc:
                    logger.debug("Reader loop select error", error=str(exc))
                    break
                if not readable:
                    continue
            with self._io_lock:
                try:
                    chunk = self.sock.recv(4096)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    continue
                except (OSError, ValueError) as exc:
                    logger.debug("Reader loop socket error", error=str(exc))
                    break
            if not chunk:
                break
            for frame in self.decoder.process(chunk):
                if frame.ok() and frame.address == 1:
                    logger.debug("Gateway RPC frame received", frame_bytes=len(frame.data))
                    self.rpc_client.process_packet(frame.data)

    def close(self) -> None:
        logger.info("Gateway connection: closing", reader_alive=self.reader_thread.is_alive())
        self.running = False
        self.reader_thread.join(timeout=2)
        with self._io_lock:
            self._closed = True
            # shutdown() sends EOF immediately so the Gateway releases the
            # client principal before any subsequent session with the same ID.
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError as exc:
                logger.debug("Socket shutdown error ignored", error=str(exc))
            try:
                self.sock.close()
            except OSError as exc:
                logger.debug("Socket close error ignored", error=str(exc))
        logger.info("Gateway connection: closed", reader_alive=self.reader_thread.is_alive())


class GatewayClient:
    """Direct client communicating with the C++ Gateway via Pigweed GatewayControl."""

    def __init__(
        self,
        connection: GatewayConnection,
        target_device_id: str,
        *,
        transfer_manager: PwTransferManager | None = None,
        default_timeout_ms: int | None = None,
    ) -> None:
        self.connection = connection
        self.target_device_id = target_device_id
        self._transfer_manager: PwTransferManager | None = transfer_manager
        self._transfer_lock = asyncio.Lock()
        env_timeout = os.environ.get("MCUBRIDGE_CLIENT_TIMEOUT_MS")
        parsed_env_timeout: int | None = None
        if env_timeout:
            try:
                parsed_env_timeout = int(env_timeout)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid MCUBRIDGE_CLIENT_TIMEOUT_MS environment variable: '{env_timeout}' must be an integer"
                ) from exc
        self.default_timeout_ms: int = (
            default_timeout_ms
            if default_timeout_ms is not None
            else (parsed_env_timeout if parsed_env_timeout is not None else 30000)
        )

    def _get_transfer_manager(self) -> PwTransferManager:
        if self._transfer_manager is None:
            transfer_service = self.connection.rpc_client.channel(1).rpcs.pw.transfer.Transfer
            self._transfer_manager = PwTransferManager(
                transfer_service,
                default_response_timeout_s=5.0,
                initial_response_timeout_s=5.0,
                max_retries=3,
                max_lifetime_retries=60,
                max_chunk_size_bytes=1024,
            )
        return self._transfer_manager

    async def transfer_read(self, resource_id: int) -> bytes:
        """Read a transfer resource using upstream Pigweed pw_transfer."""
        async with self._transfer_lock:
            manager = self._get_transfer_manager()
            return await asyncio.to_thread(manager.read, resource_id)

    async def transfer_write(self, resource_id: int, data: bytes) -> None:
        """Write a transfer resource using upstream Pigweed pw_transfer."""
        async with self._transfer_lock:
            manager = self._get_transfer_manager()
            await asyncio.to_thread(manager.write, resource_id, data)

    async def _invoke_unary(
        self,
        method: Any,
        request: ProtobufMessage | None,
        *,
        timeout_s: float,
        error_prefix: str,
        operation: str,
    ) -> Any:
        """Invoke a unary pw_rpc method from asyncio and await its response."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[PwStatus, Any]] = loop.create_future()

        def _set_result(val: tuple[PwStatus, Any]) -> None:
            if not fut.done():
                fut.set_result(val)

        # pw_rpc invokes callbacks as callback(call, arg): on_completed receives the
        # final Status (the unary response is call.response); on_error the error Status.
        # Callbacks run in reader_thread, so threadsafe dispatch into the event loop is required.
        def _on_completed(call: UnaryCall, status: PwStatus) -> None:
            logger.debug("Gateway RPC callback completed", operation=operation, status=str(status))
            loop.call_soon_threadsafe(_set_result, (status, call.response))

        def _on_error(_call: UnaryCall, error: PwStatus) -> None:
            logger.error("Gateway RPC callback failed", operation=operation, error=str(error))
            loop.call_soon_threadsafe(_set_result, (error, None))

        if request is not None:
            method.invoke(request, on_completed=_on_completed, on_error=_on_error)
        else:
            method.invoke(on_completed=_on_completed, on_error=_on_error)

        try:
            status, resp = await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError:
            logger.error(
                "Gateway RPC response timed out",
                operation=operation,
                timeout_s=timeout_s,
                target_device_id=self.target_device_id,
            )
            raise
        if not status.ok():
            raise RuntimeError(f"{error_prefix}: {status}")
        return resp

    async def dispatch(
        self,
        operation: str,
        request: Any = None,
        timeout_ms: int | None = None,
    ) -> bytes:
        """Dispatch the canonical operation (e.g. "rpc.pb.GpioService/DigitalWrite") to the target device."""
        if operation not in OPERATIONS:
            raise ValueError(f"Unknown operation: {operation}")
        payload = request.SerializeToString() if request is not None else b""
        dispatch_method = self.connection.rpc_client.channel(1).rpcs.mcubridge.gateway.GatewayControl.Dispatch
        effective_timeout_ms = timeout_ms if timeout_ms is not None else self.default_timeout_ms
        req = gateway_control_pb2.DispatchRequest(
            target_device_id=self.target_device_id,
            operation=operation,
            request_payload=payload,
            timeout_ms=effective_timeout_ms,
        )
        resp: gateway_control_pb2.DispatchResponse = await self._invoke_unary(
            dispatch_method,
            req,
            timeout_s=max(float(effective_timeout_ms) / 1000.0, 1.0) + 5.0,
            error_prefix="Pigweed RPC dispatch failed",
            operation=operation,
        )
        return resp.response_payload

    async def Health(self) -> gateway_control_pb2.HealthResponse:
        health_method = self.connection.rpc_client.channel(1).rpcs.mcubridge.gateway.GatewayControl.Health
        return await self._invoke_unary(
            health_method,
            None,
            timeout_s=5.0,
            error_prefix="Health check failed",
            operation="mcubridge.gateway.GatewayControl/Health",
        )

    async def _call[T: ProtobufMessage](
        self,
        operation: str,
        req: ProtobufMessage | None,
        response_cls: type[T],
        *,
        timeout_ms: int | None = None,
    ) -> T:
        return response_cls.FromString(await self.dispatch(operation, req, timeout_ms=timeout_ms))

    async def DigitalWrite(self, req: mcu_wire_pb2.DigitalWrite) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.GpioService/DigitalWrite", req, mcu_wire_pb2.Empty)

    async def DigitalRead(self, req: mcu_wire_pb2.PinRead) -> mcu_wire_pb2.DigitalReadResponse:
        return await self._call("rpc.pb.GpioService/DigitalRead", req, mcu_wire_pb2.DigitalReadResponse)

    async def AnalogWrite(self, req: mcu_wire_pb2.AnalogWrite) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.GpioService/AnalogWrite", req, mcu_wire_pb2.Empty)

    async def AnalogRead(self, req: mcu_wire_pb2.PinRead) -> mcu_wire_pb2.AnalogReadResponse:
        return await self._call("rpc.pb.GpioService/AnalogRead", req, mcu_wire_pb2.AnalogReadResponse)

    async def PinSubscribe(self, req: mcu_wire_pb2.PinSubscribeRequest) -> mcu_wire_pb2.PinSubscribeResponse:
        return await self._call("rpc.pb.GpioService/SubscribePins", req, mcu_wire_pb2.PinSubscribeResponse)

    async def SetPinMode(self, req: mcu_wire_pb2.PinMode) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.GpioService/SetPinMode", req, mcu_wire_pb2.Empty)

    async def ConsoleWrite(self, req: mcu_wire_pb2.ConsoleWrite) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.ConsoleService/Write", req, mcu_wire_pb2.Empty)

    async def DatastorePut(self, req: mcu_wire_pb2.DatastorePut) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.DataStoreService/Put", req, mcu_wire_pb2.Empty)

    async def DatastoreGet(self, req: mcu_wire_pb2.DatastoreGet) -> mcu_wire_pb2.DatastoreGetResponse:
        return await self._call("rpc.pb.DataStoreService/Get", req, mcu_wire_pb2.DatastoreGetResponse)

    async def MailboxPush(self, req: mcu_wire_pb2.MailboxPush) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.MailboxService/Push", req, mcu_wire_pb2.Empty)

    async def MailboxRead(self, req: mcu_wire_pb2.SubscribeRequest) -> mcu_wire_pb2.MailboxReadResponse:
        return await self._call("rpc.pb.MailboxService/Read", req, mcu_wire_pb2.MailboxReadResponse)

    async def ProcessRunAsync(self, req: mcu_wire_pb2.ProcessRunAsync) -> mcu_wire_pb2.ProcessRunAsyncResponse:
        return await self._call("rpc.pb.ProcessService/RunAsync", req, mcu_wire_pb2.ProcessRunAsyncResponse)

    async def ProcessPoll(self, req: mcu_wire_pb2.ProcessPoll) -> mcu_wire_pb2.ProcessPollResponse:
        return await self._call("rpc.pb.ProcessService/Poll", req, mcu_wire_pb2.ProcessPollResponse)

    async def ProcessKill(self, req: mcu_wire_pb2.ProcessKill) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.ProcessService/Kill", req, mcu_wire_pb2.Empty)

    async def FileRemove(self, req: mcu_wire_pb2.FileRemove) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.FileSystemService/Remove", req, mcu_wire_pb2.Empty)

    async def SpiBegin(self, req: mcu_wire_pb2.SubscribeRequest) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.SpiService/Begin", req, mcu_wire_pb2.Empty)

    async def SpiEnd(self, req: mcu_wire_pb2.SubscribeRequest) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.SpiService/End", req, mcu_wire_pb2.Empty)

    async def SpiConfigure(self, req: mcu_wire_pb2.SpiConfig) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.SpiService/SetConfig", req, mcu_wire_pb2.Empty)

    async def SpiTransfer(self, req: mcu_wire_pb2.SpiTransfer) -> mcu_wire_pb2.SpiTransferResponse:
        return await self._call("rpc.pb.SpiService/Transfer", req, mcu_wire_pb2.SpiTransferResponse)

    async def GetVersion(self, req: mcu_wire_pb2.SubscribeRequest) -> mcu_wire_pb2.VersionResponse:
        return await self._call("rpc.pb.SystemService/GetVersion", req, mcu_wire_pb2.VersionResponse)

    async def GetFreeMemory(self, req: mcu_wire_pb2.SubscribeRequest) -> mcu_wire_pb2.FreeMemoryResponse:
        return await self._call("rpc.pb.SystemService/GetFreeMemory", req, mcu_wire_pb2.FreeMemoryResponse)

    async def EnterBootloader(self, req: mcu_wire_pb2.EnterBootloader) -> mcu_wire_pb2.Empty:
        return await self._call("rpc.pb.SystemService/EnterBootloader", req, mcu_wire_pb2.Empty)

    async def push_bytes_to_device(
        self,
        data: bytes,
        remote_device_path: str,
        resource_id: int = 2,
        timeout_ms: int | None = None,
    ) -> int:
        """Push in-memory bytes to the remote OpenWrt Linux device using pw_transfer staging."""
        await self.transfer_write(resource_id, data)
        req = cloud_gateway_pb2.FileTransferSyncRequest(
            path=remote_device_path,
            resource_id=resource_id,
        )
        resp = await self._call(
            "rpc.pb.FileTransferService/PushToLinux",
            req,
            cloud_gateway_pb2.FileTransferSyncResponse,
            timeout_ms=timeout_ms,
        )
        if not resp.success:
            raise RuntimeError(f"Push to Linux device failed: {resp.message}")
        return resp.bytes_transferred

    async def fetch_bytes_from_device(
        self,
        remote_device_path: str,
        resource_id: int = 2,
        timeout_ms: int | None = None,
    ) -> bytes:
        """Fetch remote file contents as bytes from the OpenWrt Linux device using pw_transfer staging."""
        req = cloud_gateway_pb2.FileTransferSyncRequest(
            path=remote_device_path,
            resource_id=resource_id,
        )
        resp = await self._call(
            "rpc.pb.FileTransferService/FetchFromLinux",
            req,
            cloud_gateway_pb2.FileTransferSyncResponse,
            timeout_ms=timeout_ms,
        )
        if not resp.success:
            raise RuntimeError(f"Fetch from Linux device failed: {resp.message}")
        return await self.transfer_read(resource_id)

    async def push_file_to_device(
        self,
        local_path: str | os.PathLike[str],
        remote_device_path: str,
        resource_id: int = 2,
        timeout_ms: int | None = None,
    ) -> int:
        """Push a local file to the remote OpenWrt Linux device using pw_transfer staging."""
        data = Path(local_path).read_bytes()
        return await self.push_bytes_to_device(
            data=data,
            remote_device_path=remote_device_path,
            resource_id=resource_id,
            timeout_ms=timeout_ms,
        )

    async def fetch_file_from_device(
        self,
        remote_device_path: str,
        local_destination_path: str | os.PathLike[str],
        resource_id: int = 2,
        timeout_ms: int | None = None,
    ) -> int:
        """Fetch a remote file from the OpenWrt Linux device using pw_transfer staging."""
        data = await self.fetch_bytes_from_device(
            remote_device_path=remote_device_path,
            resource_id=resource_id,
            timeout_ms=timeout_ms,
        )
        dest = Path(local_destination_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return len(data)

    async def read_mcu_eeprom(self, timeout_ms: int | None = None) -> bytes:
        """Read the AVR EEPROM image of the target device (ADR-0007; needs eeprom_read)."""
        resp = await self._call(
            "rpc.pb.EepromService/Read",
            cloud_gateway_pb2.EepromSyncRequest(),
            cloud_gateway_pb2.FileTransferSyncResponse,
            timeout_ms=timeout_ms,
        )
        if not resp.success:
            raise RuntimeError(f"MCU EEPROM read failed: {resp.message}")
        return await self.transfer_read(protocol.EEPROM_TRANSFER_RESOURCE_ID)

    async def write_mcu_eeprom(self, image: bytes, timeout_ms: int | None = None) -> int:
        """Program the AVR EEPROM of the target device from offset 0 (ADR-0007; needs eeprom_write).

        Writes are not transactional and take tens of seconds for a full image;
        pass a generous timeout_ms.
        """
        if not 0 < len(image) <= protocol.EEPROM_RESOURCE_MAX_BYTES:
            raise ValueError(f"EEPROM image must be 1..{protocol.EEPROM_RESOURCE_MAX_BYTES} bytes, got {len(image)}")
        await self.transfer_write(protocol.EEPROM_TRANSFER_RESOURCE_ID, image)
        resp = await self._call(
            "rpc.pb.EepromService/Write",
            cloud_gateway_pb2.EepromSyncRequest(),
            cloud_gateway_pb2.FileTransferSyncResponse,
            timeout_ms=timeout_ms,
        )
        if not resp.success:
            raise RuntimeError(f"MCU EEPROM write failed: {resp.message}")
        return resp.bytes_transferred
