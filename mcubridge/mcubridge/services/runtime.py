"""Flattened Service Core for MCU and CLOUD orchestration. [SIL-2]"""

from __future__ import annotations

import asyncio
import functools
import logging
import shlex
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

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
from grpclib.client import Channel
from grpclib.exceptions import GRPCError, ProtocolError, StreamTerminatedError

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.mcubridge_grpc import CloudBridgeStub

from ..config.const import (
    MCU_FS_PREFIX,
    PROCESS_TERM_GRACE_PERIOD_SECONDS,
    PROP_KEY_BRIDGE_DATASTORE_KEY,
    PROP_KEY_BRIDGE_ERROR,
    PROP_KEY_BRIDGE_STATUS,
    STREAM_POLL_TIMEOUT_SECONDS,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    TOPIC_FORBIDDEN_REASON,
)
from ..config.settings import RuntimeConfig
from ..metrics import (
    publish_bridge_snapshots,
    publish_metrics,
)
from ..protocol import protocol, structures
from ..protocol.protocol import (
    Command,
    Status,
    Topic,
)
from ..protocol.structures import (
    PROTOBUF_CONTENT_TYPE,
    create_queued_publish,
    get_ssl_context,
    is_command_allowed,
    iter_chunks,
)
from ..state.context import (
    DaemonLifecycleMachine,
    FileTransferMachine,
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
from .local_bridge import ConsoleQueueTarget, LocalBridgeService
from .ubus import UbusService

__all__ = [
    "BridgeService",
    "LocalBridgeService",
    "PendingMcuRead",
]

from ..transport.serial import SerialTransport

logger = structlog.get_logger("mcubridge.service")

McuHandler = Callable[[int, bytes | ProtobufMessage], Coroutine[Any, Any, bool | bytes | ProtobufMessage | None]]


_STATUS_VALUES: Final = {s.value for s in Status}


@dataclass
class PendingMcuRead:
    future: asyncio.Future[bytes]
    chunks: list[bytes] = field(default_factory=list[bytes])
    fsm: FileTransferMachine = field(default_factory=FileTransferMachine)


class BridgeService:
    """Consolidated Service Façade Eradicating Component Wrappers. [SIL-2]"""

    config: RuntimeConfig
    state: RuntimeState
    serial: SerialTransport | None
    _cloud_channel: Channel | None
    cloud_stream: Any | None

    handshake: SerialHandshakeManager
    _storage_lock: asyncio.Lock
    _mcu_read_lock: asyncio.Lock
    pending_mcu_read: PendingMcuRead | None
    _process_slots: asyncio.Semaphore
    _cloud_publish_lock: asyncio.Lock
    cloud_spool: LmdbDeque | None
    mcu_registry: dict[int, McuHandler]
    ipc_requests: dict[bytes, asyncio.Queue[pb.CloudQueuedPublish]]
    console_queues: list[ConsoleQueueTarget]
    ubus_service: UbusService
    clock_sync: ClockSyncService
    gpio: GpioService
    local_bridge_service: LocalBridgeService
    _tg: asyncio.TaskGroup | None
    cloud_publisher: Callable[[pb.CloudQueuedPublish], Awaitable[None]] | None
    lifecycle_fsm: DaemonLifecycleMachine

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial: SerialTransport,
        *,
        cloud_publisher: Callable[[pb.CloudQueuedPublish], Awaitable[None]] | None = None,
        handshake: SerialHandshakeManager | None = None,
    ) -> None:
        self.config, self.state, self.serial = config, state, serial
        self.lifecycle_fsm = DaemonLifecycleMachine()
        self.cloud_publisher = cloud_publisher
        self._cloud_channel, self.cloud_stream = None, None
        self.watchdog: WatchdogKeepalive | None = None
        self.ipc_requests = {}
        self.console_queues = []
        self.local_bridge_service = LocalBridgeService(self)
        self.ubus_service = UbusService(self)
        self.ubus_service.start()
        self.clock_sync = ClockSyncService(self)
        self.gpio = GpioService(self)
        self._tg = None

        self.handshake = handshake or SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=derive_serial_timing(config),
            send_frame=serial.send_raw,
            enqueue_cloud=self.enqueue_cloud,
            acknowledge_frame=serial.acknowledge,
            logger_=logger,
        )

        self._storage_lock, self._mcu_read_lock, self.pending_mcu_read = asyncio.Lock(), asyncio.Lock(), None
        self._process_slots = asyncio.Semaphore(state.process_max_concurrent)
        self._cloud_publish_lock = asyncio.Lock()
        self.cloud_spool = None
        if self.config.cloud_spool_dir:
            self.cloud_spool = LmdbDeque(
                path=str(Path(self.config.cloud_spool_dir) / "spool_lmdb"), maxlen=self.state.cloud_queue_limit
            )
        # [SIL-2] O(1) MCU Dispatch Registry
        self.mcu_registry: dict[int, McuHandler] = protocol.REGISTRY.build_mcu_dispatch(self)

    async def send_mcu_ok(self, payload: bytes | ProtobufMessage = b"") -> bool:
        return bool(self.serial and await self.serial.send(Status.OK.value, payload))

    async def send_mcu_error(self, message: str) -> bool:
        return bool(self.serial and await self.serial.send(Status.ERROR.value, pb.GenericResponse(message=message)))

    async def unsupported_mcu_request(
        self, _seq: int, _payload: Any, msg: str = protocol.STATUS_REASON_COMMAND_VALIDATION_FAILED
    ) -> Any:
        if not self.serial:
            return False
        return await self.serial.send(Status.NOT_IMPLEMENTED.value, pb.GenericResponse(message=msg))

    # --- External Interface ---

    async def enqueue_cloud(self, message: pb.CloudQueuedPublish, *, reply_context: Any | None = None) -> None:
        resolved_message = structures.resolve_cloud_context(message, reply_context)
        correlation = resolved_message.correlation_data if resolved_message.HasField("correlation_data") else None
        if logger.is_enabled_for(logging.DEBUG):
            logger.debug(
                "enqueue_cloud debug info",
                topic=resolved_message.topic_name,
                correlation=correlation.hex() if correlation else None,
                in_ipc_requests=(correlation in self.ipc_requests if correlation else False),
                registered_keys=[k.hex() for k in self.ipc_requests],
            )
        if correlation and correlation in self.ipc_requests:
            self.ipc_requests[correlation].put_nowait(resolved_message)
            return

        if Topic.CONSOLE in resolved_message.topic_name:
            for q in list(self.console_queues):
                q.put_nowait(resolved_message)

        if self.cloud_publisher is not None:
            await self.cloud_publisher(resolved_message)
            return

        if not self.config.cloud_enabled or self.is_draining:
            return

        self.state.cloud_publish_queue.put_nowait(resolved_message)
        try:
            async with self._cloud_publish_lock:
                await self.flush_cloud_spool_locked()
                if await self.publish_cloud_message(resolved_message):
                    return
                if await self.spool_cloud_message_locked(resolved_message):
                    return
                self.record_cloud_drop(resolved_message.topic_name)
        finally:
            try:
                self.state.cloud_publish_queue.get_nowait()
            except asyncio.QueueEmpty:
                logger.debug("CLOUD publish queue already empty during pop")

    async def enqueue_cloud_publish(
        self,
        topic_name: str,
        payload: bytes,
        content_type: str | None = None,
        message_expiry_interval: int | None = None,
        user_properties: Iterable[tuple[str, str]] = (),
        qos: int = 1,
        *,
        reply_context: Any | None = None,
    ) -> None:
        """Create and enqueue a CloudQueuedPublish message in a single operation. [SIL-2]"""
        await self.enqueue_cloud(
            create_queued_publish(
                topic_name=topic_name,
                payload=payload,
                content_type=content_type,
                message_expiry_interval=message_expiry_interval,
                user_properties=user_properties,
                qos=qos,
            ),
            reply_context=reply_context,
        )

    async def enqueue_cloud_status_report(
        self,
        report: pb.StatusReport,
        *,
        user_properties: Iterable[tuple[str, str]] = (),
        reply_context: Any | None = None,
    ) -> None:
        """Enqueue a Protobuf StatusReport message to the cloud. [SIL-2]"""
        topic = protocol.REGISTRY.get_topic_for_message(self.state.cloud_topic_prefix, pb.StatusReport) or ""
        await self.enqueue_cloud_publish(
            topic,
            report.SerializeToString(),
            content_type=PROTOBUF_CONTENT_TYPE,
            user_properties=user_properties,
            reply_context=reply_context,
        )

    async def flush_cloud_spool(self) -> None:
        async with self._cloud_publish_lock:
            await self.flush_cloud_spool_locked()

    def record_cloud_drop(self, topic_name: str) -> None:
        self.state.cloud_drop_counts[topic_name] = self.state.cloud_drop_counts.get(topic_name, 0) + 1
        self.state.cloud_dropped_messages += 1
        self.state.metrics.cloud_messages_dropped.inc()

    async def spool_cloud_message_locked(self, message: pb.CloudQueuedPublish) -> bool:
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
        if not self.cloud_stream or spool is None:
            return

        while len(spool) > 0:
            try:
                encoded = await spool.peek()
                queued = pb.CloudQueuedPublish.FromString(encoded)
            except IndexError as exc:
                logger.error("Spool is empty during peek", error=str(exc))
                break
            except (ValueError, TypeError, ProtobufDecodeError) as exc:
                logger.error("Dropping corrupt CLOUD spool entry", error=str(exc))
                try:
                    if len(spool) > 0:
                        await spool.popleft()
                except IndexError as pop_exc:
                    logger.error("Failed to pop corrupt entry", error=str(pop_exc))
                    break
                except (lmdb.Error, OSError) as pop_exc:
                    logger.error("Database error while popping corrupt entry", error=str(pop_exc))
                    break
                self.state.cloud_spool_corrupt_dropped += 1
                continue
            except (lmdb.Error, OSError) as exc:
                self.state.cloud_spool_degraded = True
                self.state.cloud_spool_failure_reason = str(exc)
                break

            if not await self.publish_cloud_message(queued):
                break

            try:
                await spool.popleft()
            except IndexError as exc:
                logger.error("Spool was empty during popleft", error=str(exc))
                break
            except (lmdb.Error, OSError) as exc:
                self.state.cloud_spool_degraded = True
                self.state.cloud_spool_failure_reason = str(exc)
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

    async def publish_cloud_message(self, message: pb.CloudQueuedPublish) -> bool:
        if not self.cloud_stream:
            return False

        try:
            # Check if this is a command response
            if message.correlation_data:
                seq_id = int.from_bytes(message.correlation_data[:8], "big")
                envelope = pb.CloudEnvelope(
                    protocol_version=protocol.PROTOCOL_VERSION,
                    device_id=self.state.device_id,
                    sequence_id=seq_id,
                    command_response=pb.CommandResponse(
                        status_code=200,
                        payload=message.payload,
                    ),
                )
            else:
                # Telemetry report
                report = pb.TelemetryReport()
                telemetry_attr = next(
                    (
                        field_name
                        for topic_match, field_name in protocol.TELEMETRY_TOPIC_FIELD_MAP.items()
                        if topic_match in message.topic_name
                    ),
                    protocol.TELEMETRY_DEFAULT_FIELD,
                )
                setattr(report, telemetry_attr, message.payload)

                envelope = pb.CloudEnvelope(
                    protocol_version=protocol.PROTOCOL_VERSION,
                    device_id=self.state.device_id,
                    sequence_id=0,
                    telemetry=report,
                )

            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[MPU -> GATEWAY] [TOPIC:%s] [PAYLOAD:%s] [BYTES:%d]",
                    message.topic_name,
                    envelope.WhichOneof("payload"),
                    len(message.payload),
                )

            # Send CloudEnvelope via gRPC
            await self.cloud_stream.send_message(envelope)

            self.state.metrics.cloud_messages_published.inc()
            return True
        except (OSError, ProtobufSerializationError) as exc:
            logger.error("Cloud publish failure", error=str(exc))
            return False

    # --- Lifecycle ---

    def cleanup(self) -> None:
        self.ubus_service.stop()
        self.clock_sync.fsm.disconnect()

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
            version_str = (
                f"{self.state.mcu_version[0]}.{self.state.mcu_version[1]}.{self.state.mcu_version[2]}"
                if self.state.mcu_version is not None
                else "unknown"
            )
            self.ubus_service.notify("sync", {"synchronized": True, "version": version_str})
        else:
            raise ConnectionError("MCU serial link handshake synchronization failed")

    async def on_serial_disconnected(self) -> None:
        self.state.connection_fsm.disconnect()
        self.ubus_service.notify("connection_state", {"connected": False, "synchronized": False})
        for q in (self.state.pending_digital_reads, self.state.pending_analog_reads):
            q.clear()
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        self.state.link_last_nonce_counter = 0
        self.handshake.clear_handshake_expectations()
        serial = self.serial
        if serial:
            await serial.reset()

    async def reset_link(self) -> bool:
        """Reset MCU serial link and clear pending expectations. [SIL-2]"""
        if self.serial:
            await self.serial.reset()
            self.handshake.clear_handshake_expectations()
            self.state.link_last_nonce_counter = 0
            return True
        return False

    # --- Dispatchers ---

    async def handle_mcu_frame(self, command_id: int, sequence_id: int, payload: bytes | ProtobufMessage) -> None:
        serial = self.serial
        if not serial:
            return
        spec = protocol.REGISTRY.get(command_id)
        if not (
            self.state.is_synchronized or command_id in _STATUS_VALUES or (spec is not None and spec.pre_sync_allowed)
        ):
            return

        with structlog.contextvars.bound_contextvars(cmd_id=command_id, seq_id=sequence_id):
            if handler := self.mcu_registry.get(command_id):
                p = payload
                if not isinstance(p, ProtobufMessage) and spec is not None and spec.pb_class is not None:
                    p = spec.pb_class()
                    try:
                        p.ParseFromString(cast(bytes, payload))
                    except ProtobufDecodeError as exc:
                        logger.error("Failed to decode MCU Protobuf payload", error=str(exc))
                        self.state.serial_decode_errors += 1
                        await serial.send(Status.MALFORMED.value, b"")
                        return

                if await handler(sequence_id, p) is not False and command_id not in _STATUS_VALUES:
                    await serial.acknowledge(command_id, sequence_id)
            elif spec is None or spec.paired_request_id is None:
                self.state.unknown_command_count += 1
                self.state.unknown_command_last_id = command_id
                await serial.send(Status.NOT_IMPLEMENTED.value, b"")

    @staticmethod
    def _coerce_inbound_request(inbound: Any) -> pb.CloudQueuedPublish:
        if isinstance(inbound, pb.CloudQueuedPublish):
            return inbound
        props = getattr(inbound, "properties", None)
        rt = getattr(inbound, "response_topic", None) or (getattr(props, "ResponseTopic", None) if props else None)
        cd = getattr(inbound, "correlation_data", None) or (getattr(props, "CorrelationData", None) if props else None)
        return pb.CloudQueuedPublish(
            topic_name=str(getattr(inbound, "topic_name", None) or getattr(inbound, "topic", "")),
            payload=bytes(getattr(inbound, "payload", b"")),
            correlation_data=bytes(cd) if cd else b"",
            response_topic=str(rt) if rt else "",
        )

    async def handle_request(self, inbound: Any) -> None:
        request = self._coerce_inbound_request(inbound)

        topic_val = request.topic_name or getattr(request, "topic", "")
        with structlog.contextvars.bound_contextvars(topic=topic_val):
            route = protocol.REGISTRY.parse_topic(self.state.cloud_topic_prefix, topic_val)
            if not route or route.direction != "req":
                logger.warning("Unroutable cloud topic received", topic=topic_val)
                return

            spec = protocol.REGISTRY.get(route.method)
            if not spec or not spec.handler_name:
                logger.warning("Unrecognized RPC method requested", method=route.method, topic=topic_val)
                return

            if self.state.topic_authorization and not protocol.REGISTRY.allows_method(
                self.state.topic_authorization, spec
            ):
                action = route.action or route.method
                await self.reject_cloud(request, route.method, action)
                return

            if not spec.pre_sync_allowed and route.method.startswith(("gpio.", "console.", "spi.")):
                try:
                    with anyio.fail_after(float(protocol.SYNC_TIMEOUT_MS) / 1000.0):
                        await self.state.link_sync_event.wait()
                except TimeoutError:
                    logger.error("Timed out waiting for MCU link synchronization")
                    return

            try:
                resp_bytes = await self.local_bridge_service.execute_rpc(spec.name, request.payload)
                resp_topic = request.response_topic or protocol.REGISTRY.topic_path(
                    route.prefix or self.state.cloud_topic_prefix, "res", route.method
                )
                await self.enqueue_cloud_publish(
                    resp_topic,
                    resp_bytes,
                    reply_context=request,
                )
            except (RuntimeError, ValueError, OSError, ProtobufDecodeError) as exc:
                logger.error("RPC execution failed", method=route.method, error=str(exc))
                err_topic = request.response_topic or protocol.REGISTRY.topic_path(
                    route.prefix or self.state.cloud_topic_prefix, "res", route.method
                )
                await self.enqueue_cloud_publish(
                    err_topic,
                    f"error:{exc}".encode(),
                    reply_context=request,
                )

    # --- Business Logic Implementation ---

    async def handle_mcu_xon(self, _seq: int, payload: Any) -> None:
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        await self.flush_console_queue()

    async def handle_mcu_xoff(self, _seq: int, payload: Any) -> None:
        self.state.mcu_is_paused = True
        self.state.serial_tx_allowed.clear()

    async def _publish_mcu_message(
        self,
        msg: ProtobufMessage | type[ProtobufMessage],
        payload: bytes,
        *,
        content_type: str | None = None,
        message_expiry_interval: int | None = None,
        reply_context: Any | None = None,
    ) -> None:
        await self.enqueue_cloud_publish(
            protocol.REGISTRY.get_topic_for_message(self.state.cloud_topic_prefix, msg) or "",
            payload,
            content_type=content_type,
            message_expiry_interval=message_expiry_interval,
            reply_context=reply_context,
        )

    async def handle_console_write(self, _seq: int, p: pb.ConsoleWrite) -> None:
        if p.data:
            await self._publish_mcu_message(p, p.data, message_expiry_interval=protocol.CLOUD_EXPIRY_CONSOLE)

    async def store_and_publish_datastore_value(
        self, key: str, value: bytes, *, reply_context: Any | None = None
    ) -> None:
        if self.state.datastore_cache is not None:
            await self.state.datastore_cache.set(key, value)
        await self.publish_datastore_value(key, value, reply_context=reply_context)

    async def datastore_put(
        self,
        _seq_or_key: int | str | pb.DatastorePut,
        value: bytes | pb.DatastorePut | None = None,
        *,
        reply_context: Any | None = None,
    ) -> bool:
        if isinstance(_seq_or_key, pb.DatastorePut):
            k, v = _seq_or_key.key, _seq_or_key.value
        elif isinstance(value, pb.DatastorePut):
            k, v = value.key, value.value
        elif isinstance(value, bytes):
            k, v = cast(str, _seq_or_key), value
        else:
            return False
        if (
            not k
            or len(k.encode("utf-8")) > protocol.MAX_DATASTORE_KEY_LENGTH
            or len(v) > protocol.MAX_DATASTORE_VALUE_SIZE
        ):
            return False
        await self.store_and_publish_datastore_value(k, v, reply_context=reply_context)
        return True

    async def datastore_get(self, key: str) -> bytes:
        cache = self.state.datastore_cache
        if not cache:
            return b""
        res = await cache.get(key, b"")
        return res if isinstance(res, bytes) else b""

    async def handle_datastore_get(self, _seq: int, p: pb.DatastoreGet) -> bool:
        serial = self.serial
        if not serial:
            return False
        val = await self.datastore_get(p.key)
        res = await serial.send(
            Command.CMD_DATASTORE_GET_RESP.value,
            pb.DatastoreGetResponse(value=val[: protocol.MAX_DATASTORE_VALUE_SIZE]),
        )
        return bool(res)

    async def mailbox_push(self, data: bytes) -> bool:
        await self.state.mailbox_queue.append(data)
        serial = self.serial
        if serial:
            await serial.send(Command.CMD_MAILBOX_PUSH.value, pb.MailboxPush(data=data))
        return True

    async def mailbox_read(self) -> bytes:
        try:
            return await self.state.mailbox_incoming_queue.popleft()
        except IndexError:
            return b""

    async def handle_mailbox_push(self, _seq: int, p: pb.MailboxPush) -> bool:
        await self.state.mailbox_incoming_queue.append(p.data)
        await self._publish_mcu_message(p, p.data)
        return True

    async def handle_mailbox_available(self, _seq: int, p: Any) -> bool:
        serial = self.serial
        if not serial:
            return False
        count = len(self.state.mailbox_queue)
        res = await serial.send(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            pb.MailboxAvailableResponse(count=count),
        )
        return bool(res)

    async def handle_mailbox_read(self, _seq: int, p: Any) -> bool:
        serial = self.serial
        if not serial:
            return False
        content = b""
        try:
            content = await self.state.mailbox_queue.popleft()
        except IndexError:
            logger.debug("Mailbox queue empty on MCU read request")
        res = await serial.send(
            Command.CMD_MAILBOX_READ_RESP.value,
            pb.MailboxReadResponse(content=content),
        )
        return bool(res)

    async def handle_mailbox_processed(self, _seq: int, p: pb.MailboxProcessed) -> None:
        await self._publish_mcu_message(p, p.SerializeToString(), content_type=PROTOBUF_CONTENT_TYPE)

    async def file_write(self, path: str, data: bytes) -> bool:
        clean = path.lstrip("/")
        if clean.startswith(MCU_FS_PREFIX):
            mcu_path = clean.removeprefix(MCU_FS_PREFIX)
            serial = self.serial
            return bool(
                serial and await serial.send(Command.CMD_FILE_WRITE.value, pb.FileWrite(path=mcu_path, data=data))
            )
        return await self.safe_file_write(path, data)

    async def file_read(self, path: str) -> bytes | None:
        clean = path.lstrip("/")
        if clean.startswith(MCU_FS_PREFIX):
            return await self.read_mcu_file(clean)
        return await self.safe_file_read(path)

    async def file_remove(self, path: str) -> bool:
        clean = path.lstrip("/")
        if clean.startswith(MCU_FS_PREFIX):
            mcu_path = clean.removeprefix(MCU_FS_PREFIX)
            serial = self.serial
            return bool(serial and await serial.send(Command.CMD_FILE_REMOVE.value, pb.FileRemove(path=mcu_path)))
        return await self.safe_file_remove(path)

    async def handle_file_write(self, _seq: int, p: pb.FileWrite) -> bool:
        if not self.serial:
            return False
        ok = await self.safe_file_write(p.path, p.data)
        return await (self.send_mcu_ok() if ok else self.send_mcu_error("Path not allowed or quota exceeded"))

    async def handle_file_read(self, _seq: int, p: pb.FileRead) -> None:
        if not self.serial:
            return
        data = await self.safe_file_read(p.path)
        if data is None:
            await self.send_mcu_error("Read failed")
            return
        if not data:
            await self.serial.send(Command.CMD_FILE_READ_RESP.value, pb.FileReadResponse(content=b""))
        else:
            for chunk in iter_chunks(data, protocol.MAX_PAYLOAD_SIZE - 3):
                await self.serial.send(Command.CMD_FILE_READ_RESP.value, pb.FileReadResponse(content=chunk))

    async def handle_file_remove(self, _seq: int, p: pb.FileRemove) -> bool:
        if not self.serial:
            return False
        ok = await self.safe_file_remove(p.path)
        return await (self.send_mcu_ok() if ok else self.send_mcu_error("Path not allowed or not found"))

    async def handle_file_read_resp(self, _seq: int, p: pb.FileReadResponse) -> bool:
        if not self.pending_mcu_read:
            return False
        if p.content:
            self.pending_mcu_read.fsm.receive_chunk()
            self.pending_mcu_read.chunks.append(p.content)
        elif not self.pending_mcu_read.future.done():
            self.pending_mcu_read.fsm.complete()
            self.pending_mcu_read.future.set_result(b"".join(self.pending_mcu_read.chunks))
        return True

    async def handle_process_run_async(self, _seq: int, p: pb.ProcessRunAsync) -> bool:
        if not self.serial:
            return False
        allowed = is_command_allowed(self.state.allowed_policy, p.command)
        pid = (await self.run_process(p.command)) if (p.command and allowed) else 0
        if pid:
            res = await self.serial.send(
                Command.CMD_PROCESS_RUN_ASYNC_RESP.value,
                pb.ProcessRunAsyncResponse(pid=pid),
            )
            return bool(res)
        await self.send_mcu_error("Exec failed")
        return False

    async def handle_process_poll(self, _seq: int, p: pb.ProcessPoll) -> bool:
        if not self.serial:
            return False
        batch = await self.poll_process(p.pid)
        res = await self.serial.send(
            Command.CMD_PROCESS_POLL_RESP.value,
            batch,
        )
        return bool(res)

    async def handle_spi_transfer_resp(self, _seq: int, p: pb.SpiTransferResponse) -> None:
        await self._publish_mcu_message(p, p.data)

    async def handle_ack(self, _seq: int, payload: bytes | ProtobufMessage) -> None:
        try:
            if isinstance(payload, ProtobufMessage):
                p = cast(pb.AckPacket, payload)
            else:
                p = pb.AckPacket.FromString(payload)
            logger.debug("MCU ACK received", command_id=f"0x{p.command_id:02X}")
        except (ProtobufDecodeError, TypeError, ValueError) as e:
            logger.error("Failed to decode MCU ACK packet", error=str(e))

    async def handle_mcu_status(self, status: Status, seq_id: int, payload: bytes | ProtobufMessage) -> None:
        self.state.metrics.mcu_status_counts.labels(status=status.name).inc()
        text = ""
        if payload:
            try:
                if isinstance(payload, ProtobufMessage):
                    text = getattr(payload, "message", str(payload))
                else:
                    text = pb.GenericResponse.FromString(payload).message
            except (ProtobufDecodeError, TypeError, ValueError):
                if isinstance(payload, bytes):
                    try:
                        text = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        text = f"<hex:{payload.hex()}>"
                else:
                    text = str(payload)
        log_func = logger.error if status not in {Status.OK, Status.ACK} else logger.debug
        log_func("MCU > %s: %s %s", status.name, status.description, text)
        await self.enqueue_cloud_status_report(
            pb.StatusReport(
                status=status.value,
                name=status.name,
                description=status.description,
                message=text,
            ),
            user_properties=((PROP_KEY_BRIDGE_STATUS, status.name),),
        )

    # --- Low-level Helpers ---

    async def request_mcu_version(self, inbound: pb.CloudQueuedPublish | None = None) -> bool:
        if not self.serial:
            return False
        pl = await self.serial.send(Command.CMD_GET_VERSION.value, b"")
        if not isinstance(pl, (pb.VersionResponse, bytes)):
            return False
        try:
            p = pl if isinstance(pl, pb.VersionResponse) else pb.VersionResponse.FromString(pl)
        except ProtobufDecodeError as exc:
            logger.error("Failed to decode VersionResponse payload", error=str(exc))
            return False
        self.state.mcu_version = (p.major, p.minor, p.patch)

        await self._publish_mcu_message(
            pb.VersionResponse,
            f"{p.major}.{p.minor}.{p.patch}".encode(),
            message_expiry_interval=protocol.CLOUD_EXPIRY_DATASTORE,
            reply_context=inbound,
        )
        return True

    async def flush_console_queue(self) -> None:
        serial = self.serial
        if not serial:
            return
        while self.state.console_to_mcu_queue and not self.state.mcu_is_paused:
            buf = self.state.console_to_mcu_queue.popleft()

            for chunk in iter_chunks(buf, protocol.MAX_PAYLOAD_SIZE):
                if not await serial.send(Command.CMD_CONSOLE_WRITE.value, pb.ConsoleWrite(data=chunk)):
                    self.state.console_to_mcu_queue.appendleft(buf)
                    return

    async def run_process(self, command: str) -> int:
        if not is_command_allowed(self.state.allowed_policy, command):
            return 0
        with structlog.contextvars.bound_contextvars(command=command):
            await self._process_slots.acquire()
            try:
                p = await asyncio.create_subprocess_exec(
                    *shlex.split(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                pid = p.pid & protocol.UINT16_MAX
                async with self.state.process_lock:
                    self.state.running_processes[pid] = ProcessContext(p)
                tg = self._tg
                (tg.create_task if tg else asyncio.create_task)(self._monitor_process(pid))
                return pid
            except OSError:
                self._process_slots.release()
                return 0

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
                        with anyio.fail_after(float(self.state.process_timeout)):
                            ctx.exit_code = await ctx.handle.wait()
                    except TimeoutError:
                        ctx.exit_code = await self.terminate_process(
                            pid, ctx, grace_period=PROCESS_TERM_GRACE_PERIOD_SECONDS
                        )
                    await asyncio.sleep(60.0)
            finally:
                with anyio.CancelScope(shield=True):
                    self.release_process(pid)

    async def poll_process(self, pid: int) -> pb.ProcessPollResponse:
        async with self.state.process_lock:
            ctx = self.state.running_processes.get(pid)
            if not ctx:
                return pb.ProcessPollResponse(
                    status=Status.ERROR.value,
                    exit_code=1,
                    stdout_data=b"",
                    stderr_data=b"",
                    finished=True,
                    stdout_truncated=False,
                    stderr_truncated=False,
                )
            async with ctx.io_lock:

                async def _rd(s: asyncio.StreamReader | None) -> tuple[bytes, bool]:
                    if not s or s.at_eof():
                        return b"", False
                    try:
                        with anyio.move_on_after(STREAM_POLL_TIMEOUT_SECONDS):
                            data = await s.read(protocol.MAX_PAYLOAD_SIZE - 32)
                            return data, not s.at_eof()
                        logger.debug("Stream poll read timed out; process stream still pending")
                        return b"", True
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
                return pb.ProcessPollResponse(
                    status=Status.OK.value,
                    exit_code=ctx.exit_code,
                    stdout_data=o,
                    stderr_data=e,
                    finished=fin,
                    stdout_truncated=to,
                    stderr_truncated=te,
                )

    async def terminate_process(self, pid: int, ctx: ProcessContext, *, grace_period: float) -> int:
        if ctx.handle.returncode is not None:
            ctx.fsm.finish()
            return ctx.handle.returncode

        if not ctx.fsm.terminating.is_active:
            ctx.fsm.terminate()

        await anyio.to_thread.run_sync(
            lambda: terminate_pid_tree(ctx.handle.pid, timeout=grace_period),
            abandon_on_cancel=True,
        )

        ctx.fsm.finish()
        return ctx.handle.returncode if ctx.handle.returncode is not None else -1

    async def kill_process(
        self, _seq_or_pid: int | pb.ProcessKill, pid: int | pb.ProcessKill | None = None
    ) -> tuple[bool, str | None]:
        target = pid if isinstance(pid, pb.ProcessKill) else _seq_or_pid
        actual_pid = target.pid if isinstance(target, pb.ProcessKill) else int(target)
        with structlog.contextvars.bound_contextvars(pid=actual_pid):
            async with self.state.process_lock:
                ctx = self.state.running_processes.get(actual_pid)
            if not ctx:
                return False, "PID not found"
            try:
                ctx.exit_code = await self.terminate_process(
                    actual_pid, ctx, grace_period=PROCESS_TERM_GRACE_PERIOD_SECONDS
                )
                return True, None
            except OSError as exc:
                logger.error("Process termination failed", error=str(exc))
                return False, str(exc)
            finally:
                self.release_process(actual_pid)

    def _get_safe_path(self, p_str: str) -> anyio.Path | None:
        root = Path(self.config.file_system_root).resolve()
        p = root.joinpath(p_str.lstrip("/")).resolve()
        return anyio.Path(p) if p.is_relative_to(root) else None

    async def safe_file_write(self, p_str: str, data: bytes) -> bool:
        path = self._get_safe_path(p_str)
        return bool(path and await self.write_with_quota(path, data))

    async def safe_file_read(self, p_str: str) -> bytes | None:
        if not (path := self._get_safe_path(p_str)):
            return None
        try:
            return await path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            logger.debug("Safe file read failed", path=str(path), error=str(exc))
            return None

    async def safe_file_remove(self, p_str: str) -> bool:
        if not (path := self._get_safe_path(p_str)):
            return False
        try:
            await path.unlink()
            return True
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            logger.debug("Safe file remove failed", path=str(path), error=str(exc))
            return False

    async def read_mcu_file(self, target: str) -> bytes:
        serial = self.serial
        if not serial:
            return b""
        async with self._mcu_read_lock:
            pending = PendingMcuRead(asyncio.get_running_loop().create_future())
            pending.fsm.start_transfer()
            self.pending_mcu_read = pending
            clean_path = target.lstrip("/").removeprefix(MCU_FS_PREFIX)
            if not await serial.send_raw(
                Command.CMD_FILE_READ.value,
                pb.FileRead(path=clean_path),
            ):
                pending.fsm.abort()
                logger.error("MCU file read dispatch failed")
                self.pending_mcu_read = None
                return b""
            try:
                timeout_seconds = max(0.1, self.state.serial_response_timeout_ms / 1000.0)
                with anyio.fail_after(timeout_seconds):
                    return await pending.future
            except TimeoutError:
                pending.fsm.timeout()
                logger.error("Timed out waiting for MCU file read response")
                return b""
            finally:
                self.pending_mcu_read = None

    async def write_with_quota(self, path: Path | anyio.Path, data: bytes) -> bool:
        async with self._storage_lock:
            if self.state.file_write_max_bytes > 0 and len(data) > self.state.file_write_max_bytes:
                self.state.file_write_limit_rejections += 1
                return False
            try:
                usage = await anyio.to_thread.run_sync(psutil.disk_usage, self.config.file_system_root)
                self.state.file_storage_bytes_used = usage.used

                if usage.free < len(data):
                    self.state.file_storage_limit_rejections += 1
                    return False
            except OSError as exc:
                logger.error("Disk usage check failed", error=str(exc))
            p = anyio.Path(path)
            await p.parent.mkdir(parents=True, exist_ok=True)
            await p.write_bytes(data)
            return True

    async def reject_cloud(self, ctx: Any, tp: Topic | str, act: str) -> None:
        val = tp.value if isinstance(tp, Topic) else tp
        await self.enqueue_cloud_status_report(
            pb.StatusReport(status=403, topic=val, action=act, reason="forbidden"),
            user_properties=((PROP_KEY_BRIDGE_ERROR, TOPIC_FORBIDDEN_REASON),),
            reply_context=ctx,
        )

    async def publish_datastore_value(
        self, key: str, val: bytes, reply_context: Any | None = None, error: str | None = None
    ) -> None:
        tp = getattr(reply_context, "response_topic", None) or protocol.REGISTRY.topic_path(
            self.state.cloud_topic_prefix, "res", "datastore.get"
        )
        props = (
            ((PROP_KEY_BRIDGE_DATASTORE_KEY, key), (PROP_KEY_BRIDGE_ERROR, error))
            if error
            else ((PROP_KEY_BRIDGE_DATASTORE_KEY, key),)
        )
        await self.enqueue_cloud_publish(
            tp,
            val,
            message_expiry_interval=protocol.CLOUD_EXPIRY_DATASTORE,
            user_properties=props,
            reply_context=reply_context,
        )

    @property
    def is_running(self) -> bool:
        return self.lifecycle_fsm.running.is_active

    @property
    def is_draining(self) -> bool:
        return self.lifecycle_fsm.draining.is_active

    @property
    def is_ubus_active(self) -> bool:
        return self.ubus_service.is_active

    @property
    def is_snapshots_enabled(self) -> bool:
        return self.config.bridge_summary_interval > 0.0 or self.config.bridge_handshake_interval > 0.0

    @property
    def is_watchdog_enabled(self) -> bool:
        return self.config.watchdog_enabled

    def task_worker_serial(self) -> Callable[[], Awaitable[None]]:
        return self.serial.run if self.serial else lambda: asyncio.sleep(0)

    def task_worker_cloud(self) -> Callable[[], Awaitable[None]]:
        return self.run_cloud

    def task_worker_ubus(self) -> Callable[[], Awaitable[None]]:
        return self.ubus_service.run

    def task_worker_clock_sync(self) -> Callable[[], Awaitable[None]]:
        return self.clock_sync.run

    def task_worker_status_writer(self) -> Callable[[], Awaitable[None]]:
        return lambda: status_writer(self.state, self.config.status_interval)

    def task_worker_metrics_publisher(self) -> Callable[[], Awaitable[None]]:
        return lambda: publish_metrics(self.state, self.enqueue_cloud, self.config.status_interval)

    def task_worker_bridge_snapshots(self) -> Callable[[], Awaitable[None]]:
        return lambda: publish_bridge_snapshots(
            self.state,
            self.enqueue_cloud,
            summary_interval=self.config.bridge_summary_interval,
            handshake_interval=self.config.bridge_handshake_interval,
        )

    def task_worker_watchdog(self) -> Callable[[], Awaitable[None]]:
        self.watchdog = WatchdogKeepalive(interval=self.config.watchdog_interval, state=self.state)
        return self.watchdog.run

    # --- De-layered Orchestration [SIL-2] ---

    async def run(self) -> None:
        """Main entry point for daemon execution using native TaskGroup orchestration."""
        self.lifecycle_fsm.start()
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                self.lifecycle_fsm.run()
                for name, fn, fatals in protocol.REGISTRY.build_supervised_tasks(
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
            after=lambda rs: self.state.metrics.retries.labels(component="cloud_connect").inc(),
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
        logger.info("Connecting to Cloud Gateway", host=self.config.cloud_host, port=self.config.cloud_port)

        channel = Channel(
            self.config.cloud_host,
            self.config.cloud_port,
            ssl=tls_context,
        )
        self._cloud_channel = channel
        stub = CloudBridgeStub(channel)
        try:
            async with stub.Session.open(metadata={"x-device-id": self.state.device_id}) as stream:
                if self.config.cloud_http3_enabled:
                    logger.info(
                        "Attempting primary connection via gRPC over HTTP/3 (QUIC)",
                        port=self.config.cloud_http3_port,
                    )
                    self.state.cloud_fsm.connect_http3()
                    logger.info("Connected to Cloud Gateway via gRPC over HTTP/3 (QUIC).")
                else:
                    self.state.cloud_fsm.connect_http2()
                self.cloud_stream = stream
                logger.info("Connected to Cloud Gateway via gRPC.")

                # Emit status online event
                await self.send_cloud_event("status_online", "info", "Device online")
                await self.flush_cloud_spool()

                try:
                    # Read loop - [SIL-2] Direct Protobuf RPC dispatch
                    async for envelope in stream:
                        payload_type = envelope.WhichOneof("payload")
                        if logger.is_enabled_for(logging.DEBUG):
                            logger.debug(
                                "[GATEWAY -> MPU] [TYPE:%s] [SEQ:%d]",
                                payload_type,
                                envelope.sequence_id,
                            )
                        if payload_type == "pong":
                            logger.debug("Received keepalive pong from cloud.")
                            continue

                        if payload_type == "command_request":
                            cmd = envelope.command_request
                            method_name = (
                                cmd.command_path.removeprefix("rpc/")
                                if cmd.command_path.startswith("rpc/")
                                else cmd.command_path
                            )
                            try:
                                resp_payload = await self.local_bridge_service.execute_rpc(method_name, cmd.payload)
                                status_code = 200
                                err_msg = ""
                            except (ValueError, ProtobufDecodeError) as exc:
                                logger.warning("Unknown or invalid RPC method from cloud", error=str(exc))
                                resp_payload = b""
                                status_code = 404
                                err_msg = str(exc)
                            except (OSError, RuntimeError) as exc:
                                logger.error("RPC execution error on device", error=str(exc))
                                resp_payload = b""
                                status_code = 500
                                err_msg = str(exc)

                            resp_env = pb.CloudEnvelope(
                                protocol_version=protocol.PROTOCOL_VERSION,
                                device_id=self.state.device_id,
                                sequence_id=envelope.sequence_id,
                                command_response=pb.CommandResponse(
                                    status_code=status_code,
                                    error_message=err_msg,
                                    payload=resp_payload,
                                ),
                            )
                            await stream.send_message(resp_env)
                except (
                    GRPCError,
                    ProtocolError,
                    StreamTerminatedError,
                    ConnectionError,
                    OSError,
                    EOFError,
                ) as exc:
                    cur_task = asyncio.current_task()
                    if cur_task and cur_task.cancelling():
                        logger.debug("Cloud session terminated during cancellation")
                        raise asyncio.CancelledError() from None
                    logger.warning("Cloud session stream disconnected", error=str(exc))
                    raise ConnectionError(f"Cloud session stream disconnected: {exc}") from exc
        finally:
            self.state.cloud_fsm.degrade()
            self.cloud_stream = None
            self._cloud_channel = None
            channel.close()

    async def send_cloud_event(self, event_type: str, severity: str, description: str) -> None:
        envelope = pb.CloudEnvelope(
            protocol_version=protocol.PROTOCOL_VERSION,
            device_id=self.state.device_id,
            sequence_id=0,
            timestamp_utc=int(time.time()),
            event=pb.EventNotification(
                event_type=event_type,
                severity=severity,
                description=description,
            ),
        )
        if self.cloud_stream:
            if logger.is_enabled_for(logging.DEBUG):
                logger.debug(
                    "[MPU -> GATEWAY] [EVENT:%s] [SEVERITY:%s] %s",
                    event_type,
                    severity,
                    description,
                )
            await self.cloud_stream.send_message(envelope)

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
            after=lambda rs: self.state.metrics.retries.labels(component=name).inc(),
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
