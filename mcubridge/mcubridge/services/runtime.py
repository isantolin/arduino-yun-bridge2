"""Flattened Service Core for MCU and CLOUD orchestration. [SIL-2]"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import operator
import stat
import time
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, ClassVar, Final, cast

import anyio
import anyio.to_thread
import lmdb
import psutil
import structlog
import tenacity
from google.protobuf.message import (
    DecodeError as ProtobufDecodeError,
    EncodeError as ProtobufSerializationError,
    Message as ProtobufMessage,
)
import pw_hdlc.decode
import pw_hdlc.encode
import pw_rpc.packets
import pw_status
from pw_rpc.callback_client.impl import Impl as PwCallbackClientImpl
from pw_rpc.client import Client as PwRpcClient
from pw_rpc.descriptors import Channel as PwRpcChannel
from pw_rpc.internal import packet_pb2
from pw_transfer.client import Error as PwTransferError, Manager as PwTransferManager

from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
from mcubridge.protocol.cloud_gateway_pb2 import (
    CloudQueuedPublish,
    StatusReport,
)
from mcubridge.protocol.gateway import gateway_control_pb2

from ..config.settings import RuntimeConfig
from ..metrics import (
    publish_bridge_snapshots,
    publish_metrics,
)
from ..protocol import protocol
from ..protocol.protocol import (
    PROCESS_TERM_GRACE_PERIOD_SECONDS,
    STREAM_POLL_TIMEOUT_SECONDS,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    Status,
)
from ..protocol.structures import (
    get_ssl_context,
    parse_allowed_command,
)
from ..state.context import (
    DaemonLifecycleMachine,
    ProcessContext,
    RuntimeState,
    terminate_pid_tree,
)
from ..state.status import STATUS_FILE, status_writer
from ..state.storage import LmdbDeque
from ..watchdog import WatchdogKeepalive
from .clock_sync import ClockSyncService
from .gpio import GpioService
from .handshake import SerialHandshakeFatal, SerialHandshakeManager, derive_serial_timing
from .ubus import UbusService, format_ubus_bytes

__all__ = [
    "BridgeService",
]

from ..transport.serial import SerialTransport, cancel_rpc_call

logger = structlog.get_logger("mcubridge.service")

_STATUS_VALUES: Final = {s.value for s in Status}
CLOUD_TELEMETRY_TIMEOUT_SECONDS = 5.0


def _directory_usage_bytes(root: str) -> int:
    """Sum regular-file sizes under the managed storage root without following symlinks. [SIL-2]"""
    total = 0
    for dirpath, _dirnames, filenames in Path(root).walk(follow_symlinks=False):
        for name in filenames:
            try:
                st = (dirpath / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


class BridgeService:
    """Consolidated Service Façade Eradicating Component Wrappers. [SIL-2]"""

    config: RuntimeConfig
    state: RuntimeState
    serial: SerialTransport | None
    cloud_writer: asyncio.StreamWriter | None
    cloud_rpc_client: Any | None
    cloud_transfer_manager: Any | None

    handshake: SerialHandshakeManager
    _storage_lock: asyncio.Lock
    _upload_lock: asyncio.Lock
    _background_tasks: set[asyncio.Task[None]]
    _process_slots: asyncio.Semaphore
    _cloud_publish_lock: asyncio.Lock
    cloud_spool: LmdbDeque | None
    mcu_registry: protocol.McuDispatchTable
    ubus_service: UbusService
    clock_sync: ClockSyncService
    gpio: GpioService
    _tg: asyncio.TaskGroup | None
    cloud_publisher: Callable[[CloudQueuedPublish], Awaitable[None]] | None
    lifecycle_fsm: DaemonLifecycleMachine

    _MCU_ONLY_LOCAL_HANDLERS: ClassVar[frozenset[str]] = frozenset({"rpc.pb.FileSystemService/Upload"})

    LOCAL_RPC_HANDLERS: ClassVar[dict[str, str]] = {
        "rpc.pb.SystemService/LinkSync": "handshake.handle_link_sync_resp",
        "rpc.pb.SystemService/Xoff": "mcu_xoff",
        "rpc.pb.SystemService/Xon": "mcu_xon",
        "rpc.pb.FileSystemService/Remove": "file_remove",
        "rpc.pb.FileSystemService/Upload": "file_upload",
        "rpc.pb.FileTransferService/PushToLinux": "file_transfer_push_to_linux",
        "rpc.pb.FileTransferService/FetchFromLinux": "file_transfer_fetch_from_linux",
        "rpc.pb.EepromService/Read": "eeprom_read",
        "rpc.pb.EepromService/Write": "eeprom_write",
        "rpc.pb.MailboxService/Push": "mailbox_push",
        "rpc.pb.MailboxService/Read": "mailbox_read",
        "rpc.pb.MailboxService/Available": "mailbox_available",
        "rpc.pb.MailboxService/Processed": "publish_mcu_message",
        "rpc.pb.ProcessService/RunAsync": "process_run_async",
        "rpc.pb.ProcessService/Poll": "process_poll",
        "rpc.pb.ProcessService/Kill": "process_kill",
        "rpc.pb.DataStoreService/Put": "datastore_put",
        "rpc.pb.DataStoreService/Get": "datastore_get",
        "rpc.pb.ConsoleService/Write": "console_write",
        "rpc.pb.GpioService/SubscribePins": "pin_subscribe",
    }

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial: SerialTransport,
        *,
        cloud_publisher: Callable[[CloudQueuedPublish], Awaitable[None]] | None = None,
        handshake: SerialHandshakeManager | None = None,
    ) -> None:
        self.config, self.state, self.serial = config, state, serial
        self.lifecycle_fsm = DaemonLifecycleMachine()
        self.cloud_publisher = cloud_publisher
        self.cloud_writer, self.cloud_rpc_client = None, None
        self.cloud_transfer_manager: Any | None = None
        self._cloud_transfer_lock = asyncio.Lock()
        self.watchdog = (
            WatchdogKeepalive(interval=config.watchdog_interval, state=state) if config.watchdog_enabled else None
        )
        self.status_writer = functools.partial(status_writer, self.state, self.config.status_interval)
        self.metrics_publisher = functools.partial(
            publish_metrics, self.state, self.enqueue_cloud, self.config.status_interval
        )
        self.bridge_snapshots = functools.partial(
            publish_bridge_snapshots,
            self.state,
            self.enqueue_cloud,
            summary_interval=self.config.bridge_summary_interval,
            handshake_interval=self.config.bridge_handshake_interval,
        )
        self.ubus_service = UbusService(self)
        self.ubus_service.start()
        self.clock_sync = ClockSyncService(self)
        self.gpio = GpioService(self)
        self._tg = None

        self.handshake = handshake or SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=derive_serial_timing(config),
            send_frame=serial.send,
            enqueue_cloud=self.enqueue_cloud,
            logger_=logger,
        )

        self._storage_lock = asyncio.Lock()
        self._upload_lock = asyncio.Lock()
        self._background_tasks = set()
        self._process_slots = asyncio.Semaphore(state.process_max_concurrent)
        self._cloud_publish_lock = asyncio.Lock()
        self.cloud_spool = None
        if self.config.cloud_spool_dir:
            self.cloud_spool = LmdbDeque(
                path=str(Path(self.config.cloud_spool_dir) / "spool_lmdb"), maxlen=self.state.cloud_queue_limit
            )
        # [SIL-2] O(1) MCU Dispatch Table
        self.mcu_registry = protocol.build_mcu_dispatch(self)

    async def send_mcu_status(self, status: Status, seq_id: int, request_or_key: Any) -> bool:
        """Complete an MCU-initiated RPC with a status bound to its service, method and call id. [SIL-2]"""
        serial = self.serial
        spec = protocol.get_operation(request_or_key)
        if serial is None or spec is None or spec.service_id is None or spec.method_id is None:
            return False
        return await serial.send_status(status, seq_id, spec.service_id, spec.method_id)

    # --- External Interface ---

    async def enqueue_cloud(self, message: CloudQueuedPublish) -> None:
        if self.cloud_publisher is not None:
            await self.cloud_publisher(message)
            return

        if not self.config.cloud_enabled or self.lifecycle_fsm.draining.is_active:
            return

        self.state.cloud_publish_queue.put_nowait(message)
        try:
            async with self._cloud_publish_lock:
                await self.flush_cloud_spool_locked()
                if await self.publish_cloud_message(message):
                    return
                if await self.spool_cloud_message_locked(message):
                    return
                self.record_cloud_drop(message.topic_name)
        finally:
            try:
                self.state.cloud_publish_queue.get_nowait()
            except asyncio.QueueEmpty:
                logger.debug("CLOUD publish queue already empty during pop")

    async def enqueue_cloud_publish(
        self,
        topic_name: str,
        payload: bytes,
    ) -> None:
        """Create and enqueue a CloudQueuedPublish message in a single operation. [SIL-2]"""
        await self.enqueue_cloud(CloudQueuedPublish(topic_name=topic_name, payload=payload))

    async def flush_cloud_spool(self) -> None:
        async with self._cloud_publish_lock:
            await self.flush_cloud_spool_locked()

    def record_cloud_drop(self, topic_name: str) -> None:
        self.state.cloud_drop_counts[topic_name] = self.state.cloud_drop_counts.get(topic_name, 0) + 1
        self.state.cloud_dropped_messages += 1

    async def spool_cloud_message_locked(self, message: CloudQueuedPublish) -> bool:
        spool = self.cloud_spool
        if spool is None:
            return False
        try:
            encoded = message.SerializeToString()
            trimmed = (await spool.append(encoded)) or 0
            if trimmed > 0:
                self.state.cloud_spool_dropped_limit += trimmed
                self.state.cloud_spool_trim_events += 1
                self.state.cloud_spool_last_trim_unix = time.time()

            pending_count = len(spool)
            self.state.cloud_spool_degraded = False
            self.state.cloud_spool_failure_reason = None
            self.state.cloud_spool_pending_messages = pending_count
            return True
        except (lmdb.Error, OSError) as exc:
            self.state.cloud_spool_degraded = True
            self.state.cloud_spool_failure_reason = str(exc)
            return False
        except ProtobufSerializationError as exc:
            logger.error("Serialization failure for spool message", error=str(exc))
            return False

    async def flush_cloud_spool_locked(self) -> None:
        spool = self.cloud_spool
        if not (self.cloud_rpc_client and self.cloud_writer) or spool is None:
            return

        async def _pop_spool() -> bool:
            try:
                await spool.popleft()
                return True
            except IndexError as exc:
                logger.error("Spool was empty during popleft", error=str(exc))
                return False
            except (lmdb.Error, OSError) as exc:
                self.state.cloud_spool_degraded = True
                self.state.cloud_spool_failure_reason = str(exc)
                return False

        while len(spool) > 0:
            try:
                encoded = await spool.peek()
                queued = CloudQueuedPublish.FromString(encoded)
            except IndexError as exc:
                logger.error("Spool is empty during peek", error=str(exc))
                break
            except (ValueError, TypeError, ProtobufDecodeError) as exc:
                logger.error("Dropping corrupt CLOUD spool entry", error=str(exc))
                if not await _pop_spool():
                    break
                self.state.cloud_spool_corrupt_dropped += 1
                continue
            except (lmdb.Error, OSError) as exc:
                self.state.cloud_spool_degraded = True
                self.state.cloud_spool_failure_reason = str(exc)
                break

            if not await self.publish_cloud_message(queued):
                break

            if not await _pop_spool():
                break

        try:
            pending_count = len(spool)
            if not self.state.cloud_spool_degraded:
                self.state.cloud_spool_degraded = False
                self.state.cloud_spool_failure_reason = None
            self.state.cloud_spool_pending_messages = pending_count

            if pending_count == 0:
                await spool.vacuum()
        except (lmdb.Error, OSError) as exc:
            self.state.cloud_spool_degraded = True
            self.state.cloud_spool_failure_reason = str(exc)

    async def publish_cloud_message(self, message: CloudQueuedPublish) -> bool:
        if not (self.cloud_rpc_client and self.cloud_writer):
            return False
        telemetry_attr = next(
            (
                field_name
                for topic_match, field_name in protocol.TELEMETRY_TOPIC_FIELD_MAP.items()
                if topic_match in message.topic_name
            ),
            protocol.TELEMETRY_DEFAULT_FIELD,
        )
        ok = await self.send_cloud_telemetry(**{telemetry_attr: message.payload})
        if ok:
            self.state.metrics.cloud_messages_published += 1
        return ok

    # --- Lifecycle ---

    def cleanup(self) -> None:
        self.ubus_service.stop()
        self.clock_sync.fsm.disconnect()
        if self.serial is not None:
            stop_event = getattr(self.serial, "stop_event", None)
            if isinstance(stop_event, asyncio.Event):
                stop_event.set()
        self.serial = None
        # [SIL-2] Async spool close is handled by run() finally block.
        # cleanup() only nullifies the reference to prevent double-close.
        self.cloud_spool = None
        self.state.cleanup()

    async def on_serial_connected(self) -> None:
        self.state.connection_fsm.connect()
        self.ubus_service.notify("connection_state", {"connected": True})
        await self.handshake.synchronize()
        if self.state.is_synchronized:
            await self.request_mcu_version()
            await self.flush_console_queue()
            await self.gpio.resubscribe_pins()
            self.ubus_service.notify("sync", {"synchronized": True, "version": self.state.mcu_version_str})
        else:
            raise ConnectionError("MCU serial link handshake synchronization failed")

    async def on_serial_disconnected(self) -> None:
        self.state.connection_fsm.disconnect()
        self.ubus_service.notify("connection_state", {"connected": False, "synchronized": False})
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        self.handshake.clear_handshake_expectations()
        serial = self.serial
        if serial:
            await serial.reset()

    async def reset_link(self) -> bool:
        """Reset MCU serial link and clear pending expectations. [SIL-2]"""
        if self.serial:
            await self.serial.reset()
            self.handshake.clear_handshake_expectations()
            return True
        return False

    # --- Dispatchers ---

    async def handle_mcu_frame(
        self, operation_key: int | str | tuple[int, int], sequence_id: int, payload: bytes | ProtobufMessage
    ) -> None:
        """Dispatch an inbound frame by Pigweed service/method, operation name, or status code."""
        serial = self.serial
        if not serial:
            return

        spec = protocol.get_operation(operation_key)
        is_status = isinstance(operation_key, int) and operation_key in _STATUS_VALUES
        if not (self.state.is_synchronized or is_status or (spec is not None and spec.pre_sync_allowed)):
            return

        cmd_label = spec.name if spec is not None else operation_key
        with structlog.contextvars.bound_contextvars(cmd_id=cmd_label, seq_id=sequence_id):
            handler = self.mcu_registry.get(operation_key) or (self.mcu_registry.get(spec.name) if spec else None)
            if handler is not None:
                p = payload
                if not isinstance(p, ProtobufMessage) and spec is not None and spec.pb_class is not None:
                    p = spec.pb_class()
                    try:
                        p.ParseFromString(cast(bytes, payload))
                    except ProtobufDecodeError as exc:
                        logger.error("Failed to decode MCU Protobuf payload", error=str(exc))
                        self.state.serial_decode_errors += 1
                        await self.send_mcu_status(Status.INVALID_ARGUMENT, sequence_id, spec)
                        return

                res = handler(sequence_id, p)
                if inspect.isawaitable(res):
                    await res
            else:
                self.state.unknown_command_count += 1
                self.state.unknown_command_last_id = (
                    operation_key
                    if isinstance(operation_key, int)
                    else (operation_key[1] if isinstance(operation_key, tuple) else 0)
                )
                if isinstance(operation_key, tuple):
                    await serial.send_status(Status.UNIMPLEMENTED, sequence_id, operation_key[0], operation_key[1])

    # --- Business Logic & MCU Protocol Handlers ---

    async def publish_mcu_message(
        self,
        msg_or_seq: ProtobufMessage | type[ProtobufMessage] | int,
        payload_or_msg: bytes | ProtobufMessage = b"",
    ) -> None:
        if isinstance(payload_or_msg, ProtobufMessage):
            msg: ProtobufMessage | type[ProtobufMessage] = payload_or_msg
            if isinstance(payload_or_msg, mcu_wire_pb2.SpiTransferResponse):
                payload = payload_or_msg.data
            else:
                payload = payload_or_msg.SerializeToString()
        else:
            msg = cast(ProtobufMessage | type[ProtobufMessage], msg_or_seq)
            payload = payload_or_msg
        await self.enqueue_cloud_publish(
            protocol.get_topic_for_message(self.state.cloud_topic_prefix, msg) or "",
            payload,
        )

    # --- DataStore Subsystem ---

    async def datastore_put(
        self,
        req_or_key: str | mcu_wire_pb2.DatastorePut | int,
        value: bytes | mcu_wire_pb2.DatastorePut = b"",
    ) -> cloud_gateway_pb2.GenericResponse:
        src = value if isinstance(value, mcu_wire_pb2.DatastorePut) else req_or_key
        k, v = (
            (src.key, src.value)
            if isinstance(src, mcu_wire_pb2.DatastorePut)
            else (str(req_or_key), cast(bytes, value))
        )
        if (
            not k
            or len(k.encode("utf-8")) > protocol.MAX_DATASTORE_KEY_LENGTH
            or len(v) > protocol.MAX_DATASTORE_VALUE_SIZE
        ):
            return cloud_gateway_pb2.GenericResponse(status="error")
        if self.state.datastore_cache is not None:
            await self.state.datastore_cache.set(k, v)
        tp = protocol.topic_path(self.state.cloud_topic_prefix, "res", "rpc.pb.DataStoreService/Get")
        await self.enqueue_cloud_publish(tp, v)
        return cloud_gateway_pb2.GenericResponse(status="ok")

    async def datastore_get(
        self, req_or_key: str | mcu_wire_pb2.DatastoreGet | int, p: mcu_wire_pb2.DatastoreGet | None = None
    ) -> mcu_wire_pb2.DatastoreGetResponse:
        target = p if p is not None else req_or_key
        key = target.key if isinstance(target, mcu_wire_pb2.DatastoreGet) else str(target)
        cache = self.state.datastore_cache
        res = (await cache.get(key, b"")) if cache else b""
        val = res if isinstance(res, bytes) else b""
        resp = mcu_wire_pb2.DatastoreGetResponse(value=val[: protocol.MAX_DATASTORE_VALUE_SIZE])
        if p is not None and self.serial:
            await self.serial.send("rpc.pb.DataStoreService/Get", resp)
        return resp

    # --- Mailbox Subsystem ---

    async def mailbox_push(
        self, req_or_data: bytes | mcu_wire_pb2.MailboxPush | int, p: mcu_wire_pb2.MailboxPush | None = None
    ) -> mcu_wire_pb2.Empty:
        if p is not None:
            await self.state.mailbox_incoming_queue.append(p.data)
            await self.publish_mcu_message(p, p.data)
            if self.serial:
                await self.serial.send("rpc.pb.MailboxService/Push", mcu_wire_pb2.Empty())
            return mcu_wire_pb2.Empty()
        data = req_or_data.data if isinstance(req_or_data, mcu_wire_pb2.MailboxPush) else cast(bytes, req_or_data)
        # Push first. Queue only when the MCU did not take the message, so the
        # MCU can still fetch it with MailboxService/Read; queueing it after a
        # successful push delivered every cloud message twice (push + Read).
        serial = self.serial
        delivered = (
            serial is not None
            and await serial.send("rpc.pb.MailboxService/Push", mcu_wire_pb2.MailboxPush(data=data)) is not False
        )
        if not delivered:
            await self.state.mailbox_queue.append(data)
        return mcu_wire_pb2.Empty()

    async def mailbox_read(
        self, req_or_seq: mcu_wire_pb2.SubscribeRequest | int | None = None, _p: Any = None
    ) -> mcu_wire_pb2.MailboxReadResponse:
        from_mcu = isinstance(req_or_seq, int)
        queue = self.state.mailbox_queue if from_mcu else self.state.mailbox_incoming_queue
        try:
            content = await queue.popleft()
        except IndexError:
            content = b""
        resp = mcu_wire_pb2.MailboxReadResponse(content=content)
        if from_mcu and self.serial:
            await self.serial.send("rpc.pb.MailboxService/Read", resp)
        return resp

    async def mailbox_available(self, _seq: int, _p: Any) -> bool:
        return bool(
            self.serial
            and await self.serial.send(
                "rpc.pb.MailboxService/Available",
                mcu_wire_pb2.MailboxAvailableResponse(count=len(self.state.mailbox_queue)),
            )
        )

    # --- Filesystem Subsystem ---

    async def file_write(self, path: str, data: bytes) -> cloud_gateway_pb2.GenericResponse:
        """Write content directly to the local MPU file_system_root (OpenWrt UBUS IPC)."""
        safe_path = self._get_safe_path(path)
        ok = bool(safe_path and await self.write_with_quota(safe_path, data))
        return cloud_gateway_pb2.GenericResponse(status="ok" if ok else "error")

    async def file_upload(self, seq_id: int, p: mcu_wire_pb2.FileUpload) -> None:
        """ADR-0007 (E6): accept an MCU upload announced by FileSystemService/Upload.

        The bytes are read from the MCU pw_transfer resource in a separate task:
        this handler runs inside the serial read loop, which must keep feeding
        transfer packets to the pw_transfer client while the read is in flight.
        """
        safe_path = self._get_safe_path(p.path)
        limit = self.state.file_write_max_bytes
        if not safe_path:
            await self.send_mcu_status(Status.PERMISSION_DENIED, seq_id, p)
            return
        if p.size == 0 or (limit > 0 and p.size > limit):
            await self.send_mcu_status(Status.OUT_OF_RANGE, seq_id, p)
            return
        tg = self._tg
        if tg is not None:
            tg.create_task(self._complete_file_upload(seq_id, p, safe_path))
        else:
            task = asyncio.create_task(self._complete_file_upload(seq_id, p, safe_path))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _complete_file_upload(self, seq_id: int, p: mcu_wire_pb2.FileUpload, safe_path: Path) -> None:
        async with self._upload_lock:
            status = Status.OK
            if self.serial is None:
                status = Status.UNAVAILABLE
            else:
                try:
                    data = await self.serial.transfer_read(protocol.FILE_UPLOAD_RESOURCE_ID)
                except (PwTransferError, ConnectionError, OSError, RuntimeError, ValueError) as exc:
                    logger.error("MCU upload read failed", path=p.path, error=str(exc))
                    status = Status.UNAVAILABLE
                else:
                    if len(data) != p.size:
                        logger.error("MCU upload size mismatch", path=p.path, announced=p.size, received=len(data))
                        status = Status.DATA_LOSS
                    elif not await self.write_with_quota(safe_path, data):
                        status = Status.RESOURCE_EXHAUSTED
                    else:
                        logger.info("MCU upload stored", path=p.path, size=len(data))
            await self.send_mcu_status(status, seq_id, p)

    async def file_remove(
        self, path_or_req: str | mcu_wire_pb2.FileRemove | int, p: mcu_wire_pb2.FileRemove | None = None
    ) -> cloud_gateway_pb2.GenericResponse:
        src = p if p is not None else path_or_req
        path = src.path if isinstance(src, mcu_wire_pb2.FileRemove) else str(path_or_req)
        if not (safe_path := self._get_safe_path(path)):
            ok = False
        else:
            try:
                await anyio.Path(safe_path).unlink()
                ok = True
            except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
                logger.debug("Safe file remove failed", path=str(safe_path), error=str(exc))
                ok = False
        if p is not None:
            await self.send_mcu_status(Status.OK if ok else Status.NOT_FOUND, cast(int, path_or_req), src)
        return cloud_gateway_pb2.GenericResponse(status="ok" if ok else "error")

    def _get_safe_path(self, p_str: str) -> Path | None:
        if not p_str or not p_str.strip():
            return None
        root = Path(self.config.file_system_root).resolve()
        path = root.joinpath(p_str.lstrip("/")).resolve()
        return path if path != root and path.is_relative_to(root) else None

    async def _safe_file_read(self, p_str: str) -> bytes | None:
        if not (path := self._get_safe_path(p_str)):
            return None
        try:
            return await anyio.Path(path).read_bytes()
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            logger.debug("Safe file read failed", path=str(path), error=str(exc))
            return None

    async def write_with_quota(self, path: Path, data: bytes) -> bool:
        async with self._storage_lock:
            if self.state.file_write_max_bytes > 0 and len(data) > self.state.file_write_max_bytes:
                self.state.file_write_limit_rejections += 1
                return False
            try:
                used = await anyio.to_thread.run_sync(_directory_usage_bytes, self.config.file_system_root)
                self.state.file_storage_bytes_used = used
                existing = path.stat().st_size if path.is_file() else 0
                quota = self.state.file_storage_quota_bytes
                if quota > 0 and used - existing + len(data) > quota:
                    self.state.file_storage_limit_rejections += 1
                    return False
                usage = await anyio.to_thread.run_sync(psutil.disk_usage, self.config.file_system_root)
                if usage.free < len(data):
                    self.state.file_storage_limit_rejections += 1
                    return False
            except OSError as exc:
                logger.error("Disk usage check failed", error=str(exc))
            ap = anyio.Path(path)
            await ap.parent.mkdir(parents=True, exist_ok=True)
            await ap.write_bytes(data)
            return True

    def get_cloud_transfer_manager(self) -> PwTransferManager:
        if self.cloud_rpc_client is None:
            raise ConnectionError("Cloud Gateway RPC client is not connected")
        if self.cloud_transfer_manager is None:
            transfer_service = self.cloud_rpc_client.channel(1).rpcs.pw.transfer.Transfer
            self.cloud_transfer_manager = PwTransferManager(
                transfer_service,
                default_response_timeout_s=5.0,
                initial_response_timeout_s=5.0,
                max_retries=3,
                max_lifetime_retries=60,
                max_chunk_size_bytes=1024,
            )
        return cast(PwTransferManager, self.cloud_transfer_manager)

    async def cloud_transfer_read(self, resource_id: int) -> bytes:
        async with self._cloud_transfer_lock:
            manager = self.get_cloud_transfer_manager()
            return await asyncio.to_thread(manager.read, resource_id)

    async def cloud_transfer_write(self, resource_id: int, data: bytes) -> None:
        async with self._cloud_transfer_lock:
            manager = self.get_cloud_transfer_manager()
            await asyncio.to_thread(manager.write, resource_id, data)

    async def file_transfer_push_to_linux(
        self, req: cloud_gateway_pb2.FileTransferSyncRequest
    ) -> cloud_gateway_pb2.FileTransferSyncResponse:
        resource_id = req.resource_id or 2
        safe_path = self._get_safe_path(req.path)
        if not safe_path:
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message="Invalid or forbidden target path"
            )
        try:
            data = await self.cloud_transfer_read(resource_id)
        except (PwTransferError, ConnectionError, OSError, RuntimeError) as exc:
            logger.error("Cloud transfer read failed", resource_id=resource_id, error=str(exc))
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message=f"Transfer read failed: {exc}"
            )
        ok = await self.write_with_quota(safe_path, data)
        if not ok:
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message="File write quota exceeded or rejected"
            )
        return cloud_gateway_pb2.FileTransferSyncResponse(success=True, bytes_transferred=len(data), message="OK")

    async def eeprom_read(
        self, _req: cloud_gateway_pb2.EepromSyncRequest
    ) -> cloud_gateway_pb2.FileTransferSyncResponse:
        """ADR-0007: copy the MCU EEPROM image to the Gateway EEPROM staging resource."""
        if self.serial is None:
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message="MCU serial transport unavailable"
            )
        resource_id = protocol.EEPROM_TRANSFER_RESOURCE_ID
        try:
            data = await self.serial.transfer_read(resource_id)
        except (PwTransferError, ConnectionError, OSError, RuntimeError, ValueError) as exc:
            logger.error("MCU EEPROM read failed", error=str(exc))
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message=f"MCU EEPROM read failed: {exc}"
            )
        if not data or len(data) > protocol.EEPROM_RESOURCE_MAX_BYTES:
            logger.error("MCU EEPROM image has an unexpected size", size=len(data))
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message=f"Unexpected EEPROM image size: {len(data)}"
            )
        try:
            await self.cloud_transfer_write(resource_id, data)
        except (PwTransferError, ConnectionError, OSError, RuntimeError) as exc:
            logger.error("Gateway EEPROM staging write failed", error=str(exc))
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message=f"Gateway staging write failed: {exc}"
            )
        logger.info("MCU EEPROM image copied to the Gateway", size=len(data))
        return cloud_gateway_pb2.FileTransferSyncResponse(success=True, bytes_transferred=len(data), message="OK")

    async def eeprom_write(
        self, _req: cloud_gateway_pb2.EepromSyncRequest
    ) -> cloud_gateway_pb2.FileTransferSyncResponse:
        """ADR-0007: program the Gateway-staged image into the MCU EEPROM (from offset 0)."""
        if self.serial is None:
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message="MCU serial transport unavailable"
            )
        resource_id = protocol.EEPROM_TRANSFER_RESOURCE_ID
        try:
            data = await self.cloud_transfer_read(resource_id)
        except (PwTransferError, ConnectionError, OSError, RuntimeError) as exc:
            logger.error("Gateway EEPROM staging read failed", error=str(exc))
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message=f"Gateway staging read failed: {exc}"
            )
        if not data or len(data) > protocol.EEPROM_RESOURCE_MAX_BYTES:
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False,
                bytes_transferred=0,
                message=f"EEPROM image must be 1..{protocol.EEPROM_RESOURCE_MAX_BYTES} bytes, got {len(data)}",
            )
        started = time.monotonic()
        try:
            await self.serial.transfer_write(resource_id, data)
        except (PwTransferError, ConnectionError, OSError, RuntimeError, ValueError) as exc:
            # EEPROM writes are not transactional: a failure can leave a
            # partially programmed image (pw-transfer-integration.md).
            logger.error("MCU EEPROM write failed; contents may be partial", error=str(exc))
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message=f"MCU EEPROM write failed (may be partial): {exc}"
            )
        logger.info(
            "MCU EEPROM programmed from the Gateway", size=len(data), seconds=round(time.monotonic() - started, 2)
        )
        return cloud_gateway_pb2.FileTransferSyncResponse(success=True, bytes_transferred=len(data), message="OK")

    async def file_transfer_fetch_from_linux(
        self, req: cloud_gateway_pb2.FileTransferSyncRequest
    ) -> cloud_gateway_pb2.FileTransferSyncResponse:
        resource_id = req.resource_id or 2
        safe_path = self._get_safe_path(req.path)
        if not safe_path:
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message="Invalid or forbidden source path"
            )
        data = await self._safe_file_read(req.path)
        if data is None:
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message="File not found or unreadable"
            )
        try:
            await self.cloud_transfer_write(resource_id, data)
        except (PwTransferError, ConnectionError, OSError, RuntimeError) as exc:
            logger.error("Cloud transfer write failed", resource_id=resource_id, error=str(exc))
            return cloud_gateway_pb2.FileTransferSyncResponse(
                success=False, bytes_transferred=0, message=f"Transfer write failed: {exc}"
            )
        return cloud_gateway_pb2.FileTransferSyncResponse(success=True, bytes_transferred=len(data), message="OK")

    # --- Process Subsystem ---

    async def process_run_async(
        self, req_or_cmd: str | mcu_wire_pb2.ProcessRunAsync | int, p: mcu_wire_pb2.ProcessRunAsync | None = None
    ) -> mcu_wire_pb2.ProcessRunAsyncResponse:
        src = p if p is not None else req_or_cmd
        command = src.command if isinstance(src, mcu_wire_pb2.ProcessRunAsync) else str(req_or_cmd)
        pid = 0
        # Parse once: the argv checked against the policy is the argv executed.
        # Malformed quoting is rejected here, before a process slot is taken.
        argv = parse_allowed_command(self.state.allowed_policy, command)
        allowed = argv is not None
        if argv is not None:
            with structlog.contextvars.bound_contextvars(command=command):
                await self._process_slots.acquire()
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                    # The real PID is the handle: the wire field is uint32 and
                    # the firmware stores int32, so truncating it could collide
                    # with a running process or produce 0 (= failure).
                    pid = proc.pid
                    async with self.state.process_lock:
                        self.state.running_processes[pid] = ProcessContext(proc)
                    tg = self._tg
                    (tg.create_task if tg else asyncio.create_task)(self._monitor_process(pid))
                except (OSError, ValueError) as exc:
                    # OSError: missing/unexecutable program; ValueError: e.g. an
                    # embedded NUL byte. Either way the slot must be returned.
                    logger.warning("Process spawn failed", error=str(exc))
                    self._process_slots.release()
        resp = mcu_wire_pb2.ProcessRunAsyncResponse(pid=pid)
        if p is not None and self.serial:
            if pid:
                await self.serial.send("rpc.pb.ProcessService/RunAsync", resp)
            else:
                await self.send_mcu_status(
                    Status.INTERNAL if allowed else Status.PERMISSION_DENIED, cast(int, req_or_cmd), src
                )
        return resp

    async def process_poll(
        self, req_or_pid: int | mcu_wire_pb2.ProcessPoll, p: mcu_wire_pb2.ProcessPoll | None = None
    ) -> mcu_wire_pb2.ProcessPollResponse:
        src = p if p is not None else req_or_pid
        pid = src.pid if isinstance(src, mcu_wire_pb2.ProcessPoll) else src
        async with self.state.process_lock:
            ctx = self.state.running_processes.get(pid)
            if not ctx:
                resp = mcu_wire_pb2.ProcessPollResponse(
                    status=Status.INTERNAL.value,
                    exit_code=1,
                    stdout_data=b"",
                    stderr_data=b"",
                    finished=True,
                    stdout_truncated=False,
                    stderr_truncated=False,
                )
            else:
                async with ctx.io_lock:

                    async def _rd(s: asyncio.StreamReader | None) -> tuple[bytes, bool]:
                        if not s or s.at_eof():
                            return b"", False
                        try:
                            async with asyncio.timeout(STREAM_POLL_TIMEOUT_SECONDS):
                                data = await s.read(protocol.MAX_PAYLOAD_SIZE - 32)
                                return data, not s.at_eof()
                        except TimeoutError as exc:
                            logger.debug("Stream poll read timed out; process stream still pending", error=str(exc))
                            return b"", True

                    o, to = await _rd(ctx.handle.stdout)
                    e, te = await _rd(ctx.handle.stderr)
                    fin = ctx.handle.returncode is not None
                    if fin:
                        ctx.fsm.finish()
                    if (
                        fin
                        and (ctx.handle.stdout is None or ctx.handle.stdout.at_eof())
                        and (ctx.handle.stderr is None or ctx.handle.stderr.at_eof())
                    ):
                        self.release_process(pid)
                    resp = mcu_wire_pb2.ProcessPollResponse(
                        status=Status.OK.value,
                        exit_code=ctx.exit_code,
                        stdout_data=o,
                        stderr_data=e,
                        finished=fin,
                        stdout_truncated=to,
                        stderr_truncated=te,
                    )
        if p is not None and self.serial:
            await self.serial.send("rpc.pb.ProcessService/Poll", resp)
        return resp

    def release_process(self, pid: int) -> ProcessContext | None:
        ctx = self.state.running_processes.pop(pid, None)
        if ctx is not None:
            ctx.fsm.finish()
            self._process_slots.release()
        return ctx

    async def _monitor_process(self, pid: int) -> None:
        with structlog.contextvars.bound_contextvars(pid=pid):
            try:
                async with self.state.process_lock:
                    ctx = self.state.running_processes.get(pid)
                if ctx:
                    try:
                        async with asyncio.timeout(float(self.state.process_timeout)):
                            ctx.exit_code = await ctx.handle.wait()
                    except TimeoutError:
                        ctx.exit_code = await self.terminate_process(
                            pid, ctx, grace_period=PROCESS_TERM_GRACE_PERIOD_SECONDS
                        )
                    await asyncio.sleep(float(protocol.DEFAULT_PROCESS_RETENTION_SECONDS))
            finally:
                with anyio.CancelScope(shield=True):
                    self.release_process(pid)

    async def terminate_process(self, pid: int, ctx: ProcessContext, *, grace_period: float) -> int:
        if ctx.handle.returncode is not None:
            ctx.fsm.finish()
            return ctx.handle.returncode

        if not ctx.fsm.terminating.is_active:
            ctx.fsm.terminate()

        await asyncio.to_thread(terminate_pid_tree, ctx.handle.pid, timeout=grace_period)

        ctx.fsm.finish()
        return ctx.handle.returncode if ctx.handle.returncode is not None else -1

    async def process_kill(
        self, req_or_pid: int | mcu_wire_pb2.ProcessKill, p: mcu_wire_pb2.ProcessKill | None = None
    ) -> cloud_gateway_pb2.GenericResponse:
        target = p if p is not None else req_or_pid
        actual_pid = target.pid if isinstance(target, mcu_wire_pb2.ProcessKill) else target
        with structlog.contextvars.bound_contextvars(pid=actual_pid):
            async with self.state.process_lock:
                ctx = self.state.running_processes.get(actual_pid)
            if not ctx:
                return cloud_gateway_pb2.GenericResponse(status="error")
            try:
                ctx.exit_code = await self.terminate_process(
                    actual_pid, ctx, grace_period=PROCESS_TERM_GRACE_PERIOD_SECONDS
                )
                return cloud_gateway_pb2.GenericResponse(status="ok")
            except OSError as exc:
                logger.error("Process termination failed", error=str(exc))
                return cloud_gateway_pb2.GenericResponse(status="error")
            finally:
                self.release_process(actual_pid)

    # --- System, Console & Peripheral Subsystem ---

    async def flush_console_queue(self) -> None:
        serial = self.serial
        if not serial:
            return
        while self.state.console_to_mcu_queue and not self.state.mcu_is_paused:
            buf = self.state.console_to_mcu_queue.popleft()

            max_chunk = protocol.MAX_PAYLOAD_SIZE
            for i in range(0, len(buf), max_chunk):
                end = i + max_chunk
                if not await serial.send("rpc.pb.ConsoleService/Write", mcu_wire_pb2.ConsoleWrite(data=buf[i:end])):
                    self.state.console_to_mcu_queue.appendleft(buf)
                    return

    async def console_write(
        self, req_or_seq: mcu_wire_pb2.ConsoleWrite | int, p: mcu_wire_pb2.ConsoleWrite | None = None
    ) -> mcu_wire_pb2.Empty:
        """Bridge console bytes: MCU frames are published to the cloud, Linux requests are queued to the MCU."""
        if p is not None:
            if p.data:
                await self.publish_mcu_message(p, p.data)
            return mcu_wire_pb2.Empty()
        req = cast(mcu_wire_pb2.ConsoleWrite, req_or_seq)
        if req.data:
            self.state.console_to_mcu_queue.append(req.data)
            await self.flush_console_queue()
        return mcu_wire_pb2.Empty()

    async def set_mcu_paused(self, paused: bool, _seq: int = 0, _payload: Any = None) -> None:
        self.state.mcu_is_paused = paused
        if paused:
            self.state.serial_tx_allowed.clear()
        else:
            self.state.serial_tx_allowed.set()
            await self.flush_console_queue()

    mcu_xoff = functools.partialmethod(set_mcu_paused, True)
    mcu_xon = functools.partialmethod(set_mcu_paused, False)

    async def handle_mcu_status(self, status: Status, seq_id: int, payload: bytes | ProtobufMessage) -> None:
        self.state.metrics.mcu_status_counts[status.name] += 1
        text = ""
        if payload:
            text = (
                format_ubus_bytes(payload) if isinstance(payload, bytes) else getattr(payload, "message", str(payload))
            )
        log_func = logger.error if not status.ok() else logger.debug
        log_func("MCU > %s %s", status.name, text)
        report = StatusReport(
            status=status.value,
            name=status.name,
            description=status.name,
            message=text,
        )
        topic = protocol.get_topic_for_message(self.state.cloud_topic_prefix, StatusReport) or ""
        await self.enqueue_cloud_publish(topic, report.SerializeToString())

    async def _fetch_mcu_version(self) -> mcu_wire_pb2.VersionResponse | None:
        serial = self.serial
        res = (await serial.send("rpc.pb.SystemService/GetVersion", b"")) if serial else None
        if not isinstance(res, mcu_wire_pb2.VersionResponse):
            return None
        self.state.mcu_version = (res.major, res.minor, res.patch)
        return res

    async def request_mcu_version(self) -> bool:
        if await self._fetch_mcu_version() is None:
            return False
        await self.publish_mcu_message(
            mcu_wire_pb2.VersionResponse,
            self.state.mcu_version_str.encode(),
        )
        return True

    # --- De-layered Orchestration [SIL-2] ---

    async def run(self) -> None:
        """Main entry point for daemon execution using native TaskGroup orchestration."""
        self.lifecycle_fsm.start()
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                self.lifecycle_fsm.run()
                for name, fn, fatals in protocol.build_supervised_tasks(
                    self, fatal_exception_map={"SerialHandshakeFatal": SerialHandshakeFatal}
                ):
                    tg.create_task(self.supervise(name, fn, fatals))
        except BaseExceptionGroup as eg:
            cancelled_group, rest = eg.split(asyncio.CancelledError)
            if cancelled_group is not None:
                logger.info("Daemon shutdown initiated (Cancelled).")
            if rest is not None:
                for e in rest.exceptions:
                    logger.critical("Fatal task error", error=str(e), exc_info=True)
                raise rest from eg
        except asyncio.CancelledError:
            logger.info("Daemon shutdown initiated (Cancelled).")
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            tenacity.RetryError,
        ) as exc:
            logger.critical("Fatal task error", error=str(exc), exc_info=True)
            raise
        finally:
            self.lifecycle_fsm.drain()
            with anyio.CancelScope(shield=True):
                if self.cloud_spool is not None:
                    try:
                        await self.cloud_spool.close()
                    except (lmdb.Error, OSError) as exc:
                        logger.debug("cloud_spool close failed during teardown", error=str(exc))
                    self.cloud_spool = None
                self.cleanup()
                STATUS_FILE.unlink(missing_ok=True)
                self.lifecycle_fsm.stop()
                logger.info("MCU Bridge daemon stopped.")

    async def run_cloud(self) -> None:
        if not self.config.cloud_enabled:
            logger.info("Cloud transport is DISABLED in configuration.")
            self.state.cloud_fsm.disable()
            return

        tls_context = get_ssl_context(self.config)
        reconnect_delay = max(1, self.config.reconnect_delay)

        def _before_sleep(rs: tenacity.RetryCallState) -> None:
            self.state.cloud_fsm.start_reconnect()
            logger.info(
                "Cloud connection retry",
                attempt=rs.attempt_number,
                wait=getattr(rs.next_action, "sleep", 0),
            )

        retryer = tenacity.AsyncRetrying(
            wait=tenacity.wait_exponential(multiplier=reconnect_delay, max=60) + tenacity.wait_random(0, 2),
            retry=tenacity.retry_if_exception_type(
                (
                    ConnectionError,
                    OSError,
                    TimeoutError,
                )
            ),
            before_sleep=_before_sleep,
            after=lambda rs: self.state.metrics.retries.update(("cloud_connect",)),
            reraise=True,
        )

        try:
            await retryer(functools.partial(self.connect_cloud_session, tls_context))
        except asyncio.CancelledError:
            self.state.cloud_fsm.disable()
            logger.info("Cloud transport stopping.")
            raise
        except (TimeoutError, ConnectionError, OSError) as exc:
            self.state.cloud_fsm.degrade()
            logger.critical("Cloud transport fatal error", error=str(exc))
            raise

    async def connect_cloud_session(self, tls_context: Any) -> None:
        self.state.cloud_fsm.start_connecting()
        logger.info(
            "Connecting to Cloud Gateway via Pigweed over TLS",
            host=self.config.cloud_host,
            port=self.config.cloud_port,
        )

        try:
            reader, writer = await asyncio.open_connection(
                self.config.cloud_host,
                self.config.cloud_port,
                ssl=tls_context,
            )
        except (TimeoutError, ConnectionError, OSError) as exc:
            self.state.cloud_fsm.degrade()
            logger.warning("Cloud Gateway connection failed", error=str(exc))
            raise ConnectionError(f"Cloud Gateway connection failed: {exc}") from exc

        self.cloud_writer = writer

        def _send_channel_packet(packet: bytes) -> None:
            if self.cloud_writer and not self.cloud_writer.is_closing():
                frame = pw_hdlc.encode.ui_frame(1, packet)
                self.cloud_writer.write(frame)

        self.cloud_rpc_client = PwRpcClient(
            PwCallbackClientImpl(),
            [PwRpcChannel(1, _send_channel_packet)],
            protocol.PW_SERVICES,
        )

        self.state.cloud_fsm.connect_tls()
        logger.info("Connected to Cloud Gateway via Pigweed HDLC over TLS.")

        # The reader below is the only consumer of Gateway responses and EdgeControl
        # requests, so nothing that awaits a Gateway response may run before it starts.
        session_tasks: set[asyncio.Task[None]] = set()

        def _spawn(coro: Coroutine[Any, Any, None], name: str) -> None:
            task = asyncio.create_task(coro, name=name)
            session_tasks.add(task)
            task.add_done_callback(session_tasks.discard)

        async def _announce_online() -> None:
            await self.send_cloud_event("status_online", "info", "Device online")
            await self.flush_cloud_spool()

        _spawn(_announce_online(), "mcubridge-cloud-online")

        decoder = pw_hdlc.decode.FrameDecoder()
        try:
            while not self.lifecycle_fsm.stopped.is_active:
                chunk = await reader.read(4096)
                if not chunk:
                    raise ConnectionError("Cloud Gateway connection closed by peer")
                for frame in decoder.process(chunk):
                    if not frame.ok() or frame.address != 1:
                        continue
                    try:
                        pkt = pw_rpc.packets.decode(frame.data)
                    except (ValueError, ProtobufDecodeError) as exc:
                        logger.warning("Failed to decode Pigweed RPC packet from cloud", error=str(exc))
                        continue

                    if pkt.type == packet_pb2.PacketType.REQUEST:
                        _spawn(self.handle_cloud_edge_dispatch(pkt, writer), "mcubridge-edge-dispatch")
                    elif pkt.type in (
                        packet_pb2.PacketType.RESPONSE,
                        packet_pb2.PacketType.SERVER_ERROR,
                        packet_pb2.PacketType.SERVER_STREAM,
                    ):
                        logger.debug(
                            "Gateway RPC response packet received",
                            packet_type=int(pkt.type),
                            call_id=pkt.call_id,
                            channel_id=pkt.channel_id,
                            service_id=pkt.service_id,
                            method_id=pkt.method_id,
                            payload_bytes=len(pkt.payload),
                        )
                        self.cloud_rpc_client.process_packet(frame.data)
        except (ConnectionError, OSError, EOFError) as exc:
            cur_task = asyncio.current_task()
            if cur_task and cur_task.cancelling():
                logger.debug("Cloud session terminated during cancellation")
                raise asyncio.CancelledError() from None
            logger.warning("Cloud session stream disconnected", error=str(exc))
            raise ConnectionError(f"Cloud session stream disconnected: {exc}") from exc
        finally:
            for task in tuple(session_tasks):
                task.cancel()
            self.state.cloud_fsm.degrade()
            self.cloud_writer = None
            self.cloud_rpc_client = None
            self.cloud_transfer_manager = None
            try:
                writer.close()
                await writer.wait_closed()
            except OSError as exc:
                logger.debug("Cloud session writer close error ignored", error=str(exc))

    async def handle_cloud_edge_dispatch(self, pkt: Any, writer: asyncio.StreamWriter) -> None:
        edge_req = gateway_control_pb2.EdgeDispatchRequest()
        try:
            edge_req.ParseFromString(pkt.payload)
            target = edge_req.operation
            spec = protocol.BY_NAME.get(target)
            if spec is None:
                raise ValueError(f"Unknown RPC method: {target}")
            if not protocol.allows_method(self.state.topic_authorization, spec):
                # Same ACL as the topic path: cloud_allow_* flags apply to Gateway dispatch too.
                # ADR-0007: a missing authorization denies (deny-by-default).
                logger.warning("RPC method forbidden by topic authorization", method=target)
                resp_payload = b""
                status_code = 403
            else:
                resp_payload = await self.execute_rpc(target, edge_req.request_payload, timeout_ms=edge_req.timeout_ms)
                status_code = 200
        except (ValueError, ProtobufDecodeError) as exc:
            logger.warning("Unknown or invalid RPC method from cloud", error=str(exc))
            resp_payload = b""
            status_code = 404
        except (OSError, RuntimeError) as exc:
            logger.error("RPC execution error on device", error=str(exc))
            resp_payload = b""
            status_code = 500

        edge_resp = gateway_control_pb2.EdgeDispatchResponse(
            status_code=status_code,
            response_payload=resp_payload,
        )
        resp_pkt = pw_rpc.packets.encode_response(pkt, response=edge_resp, status=pw_status.Status.OK)
        frame = pw_hdlc.encode.ui_frame(1, resp_pkt)
        try:
            writer.write(frame)
            await writer.drain()
        except OSError as exc:
            logger.warning("Failed to send edge dispatch response to cloud", error=str(exc))
            return
        logger.debug(
            "Edge dispatch response sent",
            operation=edge_req.operation,
            status_code=status_code,
            call_id=pkt.call_id,
            frame_bytes=len(frame),
        )

    async def send_cloud_telemetry(
        self,
        daemon_metrics_blob: bytes = b"",
        system_status_blob: bytes = b"",
        bridge_snapshot_blob: bytes = b"",
        handshake_snapshot_blob: bytes = b"",
    ) -> bool:
        if not self.cloud_rpc_client or not self.cloud_writer:
            return False
        call: Any = None
        try:
            req = gateway_control_pb2.TelemetryReport(
                daemon_metrics_blob=daemon_metrics_blob,
                system_status_blob=system_status_blob,
                bridge_snapshot_blob=bridge_snapshot_blob,
                handshake_snapshot_blob=handshake_snapshot_blob,
            )
            report_method = self.cloud_rpc_client.channel(1).rpcs.mcubridge.gateway.DeviceTelemetry.ReportTelemetry
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[Any] = loop.create_future()

            def _on_completed(_call: Any, status: Any) -> None:
                if not fut.done():
                    fut.set_result(status)

            def _on_error(_call: Any, error: Any) -> None:
                # pw_rpc invokes every callback as callback(call, arg); error is the failure Status.
                if not fut.done():
                    fut.set_result(error)

            call = report_method.invoke(req, on_completed=_on_completed, on_error=_on_error)
            await self.cloud_writer.drain()
            async with asyncio.timeout(CLOUD_TELEMETRY_TIMEOUT_SECONDS):
                status = await fut
                return bool(status.ok())
        except asyncio.CancelledError:
            cancel_rpc_call(call, "Cloud telemetry RPC cancellation raced with task cancellation")
            raise
        except (TimeoutError, OSError, RuntimeError) as exc:
            cancel_rpc_call(call, "Cloud telemetry RPC cancellation raced with completion")
            logger.error("Failed to report telemetry to cloud", error=str(exc))
            return False

    async def send_cloud_event(self, event_type: str, severity: str, description: str) -> None:
        if logger.is_enabled_for(logging.DEBUG):
            logger.debug(
                "[MPU -> GATEWAY] [EVENT:%s] [SEVERITY:%s] %s",
                event_type,
                severity,
                description,
            )
        if self.cloud_rpc_client and self.cloud_writer:
            event_blob = f"{event_type}:{severity}:{description}".encode()
            await self.send_cloud_telemetry(system_status_blob=event_blob)

    async def supervise(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        fatal_exceptions: tuple[type[BaseException], ...] = (),
        max_restarts: int | None = None,
        min_backoff: float = SUPERVISOR_DEFAULT_MIN_BACKOFF,
        max_backoff: float = SUPERVISOR_DEFAULT_MAX_BACKOFF,
        jitter: float = 1.0,
    ) -> None:
        """[SIL-2] Supervise a critical daemon task with automatic restarts using tenacity."""

        log = logger.bind(task=name)

        retryer = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(max_restarts) if max_restarts else tenacity.stop_never,
            wait=(
                tenacity.wait_exponential(multiplier=min_backoff, max=max_backoff) + tenacity.wait_random(0, jitter)
                if jitter > 0
                else tenacity.wait_exponential(multiplier=min_backoff, max=max_backoff)
            ),
            retry=tenacity.retry_if_not_exception_type((asyncio.CancelledError, *fatal_exceptions)),
            before_sleep=lambda rs: log.error(
                "Task supervisor restarting",
                attempt=rs.attempt_number,
                error=str(rs.outcome.exception()) if rs.outcome else None,
            ),
            after=lambda rs: self.state.metrics.retries.update((name,)),
            reraise=True,
        )

        try:
            log.debug("Supervisor starting task")

            # [SIL-2] The indirection via _task_runner is required: tenacity calls
            # factory() to create the awaitable BEFORE awaiting it. If TaskGroup
            # cancellation arrives between creation and await, the coroutine leaks
            # (RuntimeWarning: coroutine never awaited). The wrapper defers factory()
            # to inside the async context so it is immediately consumed.
            async def _task_runner() -> None:
                await factory()

            await retryer(_task_runner)
        except asyncio.CancelledError:
            log.debug("Supervisor task cancelled")
            raise
        except fatal_exceptions as exc:
            log.critical("Supervisor task failed with fatal exception", error=str(exc))
            raise
        except (RuntimeError, ValueError, OSError, tenacity.RetryError) as exc:
            log.critical("Supervisor task failed unexpectedly", error=str(exc))
            raise

    # --- RPC Execution & Operations [SIL-2] ---

    async def execute_rpc(self, target: str, payload_bytes: bytes, timeout_ms: int = 0) -> bytes:
        spec = protocol.BY_NAME.get(target)
        if not spec or not spec.request_type:
            raise ValueError(f"Unknown RPC method: {target}")

        req = spec.request_type()
        if payload_bytes:
            req.ParseFromString(payload_bytes)

        with structlog.contextvars.bound_contextvars(rpc_method=target):
            if target in self._MCU_ONLY_LOCAL_HANDLERS:
                raise ValueError(f"MCU-only RPC method cannot be invoked from cloud: {target}")
            if target in self.LOCAL_RPC_HANDLERS:
                handler_path = self.LOCAL_RPC_HANDLERS[target]
                fn = operator.attrgetter(handler_path)(self)
                res = fn(req)
                resp = await res if inspect.isawaitable(res) else res
                return resp.SerializeToString() if resp is not None else b""

            if self.serial is None:
                raise ConnectionError(f"Serial link disconnected for {target}")

            payload = b"" if isinstance(req, mcu_wire_pb2.SubscribeRequest) else req
            if timeout_ms > 0:
                res = await self.serial.send(spec.name, payload, timeout=float(timeout_ms) / 1000.0)
            else:
                res = await self.serial.send(spec.name, payload)
            if isinstance(res, mcu_wire_pb2.VersionResponse):
                self.state.mcu_version = (res.major, res.minor, res.patch)
            if res is True:
                resp_cls = spec.response_type or mcu_wire_pb2.Empty
                return resp_cls().SerializeToString()
            if isinstance(res, ProtobufMessage):
                return res.SerializeToString()
            if isinstance(res, bytes):
                return res
            raise RuntimeError(f"Serial RPC invocation failed: {target}")

    async def pin_subscribe(
        self,
        req_or_seq: mcu_wire_pb2.PinSubscribeRequest | int,
        p: mcu_wire_pb2.PinSubscribeRequest | None = None,
    ) -> mcu_wire_pb2.PinSubscribeResponse:
        req = p if p is not None else cast(mcu_wire_pb2.PinSubscribeRequest, req_or_seq)
        try:
            mode_str = config_schema_pb2.PinModeType.Name(req.mode).removeprefix("PIN_")
        except (ValueError, KeyError) as exc:
            logger.warning("Unrecognized pin mode enum, defaulting to INPUT", mode=req.mode, error=str(exc))
            mode_str = "INPUT"
        res = await self.gpio.subscribe_pin(
            pin=req.pin, mode=mode_str, interval_ms=req.interval_ms, hysteresis=req.hysteresis, enabled=req.enabled
        )
        return mcu_wire_pb2.PinSubscribeResponse(pin=req.pin, success=bool(res.get("status") == "ok"))
