"""Flattened Service Core for MCU and MQTT orchestration. [SIL-2]"""

from __future__ import annotations
from mcubridge.protocol import mcubridge_pb2 as pb

import asyncio
import collections
import os

import signal
import shlex
import time
from collections.abc import Coroutine, Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, Final

import msgspec
from ..state.storage import DbmDeque
import structlog
from google.protobuf.message import DecodeError as ProtobufDecodeError, Message as ProtobufMessage

import aiomqtt

from aiomqtt.message import Message

from ..config.const import (
    MQTT_EXPIRY_CONSOLE,
    MQTT_EXPIRY_DATASTORE,
    MQTT_EXPIRY_PIN,
    TOPIC_FORBIDDEN_REASON,
)
from ..config.settings import RuntimeConfig
from ..protocol import protocol, structures
from ..protocol.protocol import (
    Command,
    ConsoleAction,
    DatastoreAction,
    FileAction,
    MailboxAction,
    PinAction,
    ShellAction,
    SpiAction,
    Status,
    SystemAction,
    response_to_request,
)

from ..protocol.structures import (
    ProcessOutputBatch,
    PROTOBUF_CONTENT_TYPE,
    QueuedPublish,
    TopicRoute,
)
from ..protocol.topics import Topic, parse_topic, topic_path
from ..state.context import ProcessContext, RuntimeState

_COMMAND_TO_PB = {
    Command.CMD_CONSOLE_WRITE.value: pb.ConsoleWrite,
    Command.CMD_DATASTORE_PUT.value: pb.DatastorePut,
    Command.CMD_DATASTORE_GET.value: pb.DatastoreGet,
    Command.CMD_MAILBOX_PUSH.value: pb.MailboxPush,
    Command.CMD_MAILBOX_PROCESSED.value: pb.MailboxProcessed,
    Command.CMD_FILE_WRITE.value: pb.FileWrite,
    Command.CMD_FILE_READ.value: pb.FileRead,
    Command.CMD_FILE_REMOVE.value: pb.FileRemove,
    Command.CMD_FILE_READ_RESP.value: pb.FileReadResponse,
    Command.CMD_PROCESS_RUN_ASYNC.value: pb.ProcessRunAsync,
    Command.CMD_PROCESS_POLL.value: pb.ProcessPoll,
    Command.CMD_PROCESS_KILL.value: pb.ProcessKill,
    Command.CMD_DIGITAL_READ_RESP.value: pb.DigitalReadResponse,
    Command.CMD_ANALOG_READ_RESP.value: pb.AnalogReadResponse,
    Command.CMD_SPI_TRANSFER_RESP.value: pb.SpiTransferResponse,
}

if TYPE_CHECKING:
    from ..transport.serial import SerialTransport
    from .handshake import SerialHandshakeManager


logger = structlog.get_logger("mcubridge.service")

McuHandler = Callable[[int, bytes | ProtobufMessage], Coroutine[Any, Any, bool | bytes | ProtobufMessage | None]]


_PRE_SYNC_ALLOWED_COMMANDS: Final = {
    Command.CMD_LINK_SYNC_RESP.value,
    Command.CMD_LINK_RESET_RESP.value,
}

_STATUS_VALUES: Final = {s.value for s in Status}


@dataclass
class _PendingMcuRead:
    identifier: str
    future: asyncio.Future[bytes]
    chunks: list[bytes] = field(default_factory=list[bytes])


class BridgeService:
    """Consolidated Service Façade Eradicating Component Wrappers. [SIL-2]"""

    config: RuntimeConfig
    state: RuntimeState
    serial: SerialTransport | None
    _mqtt_client: aiomqtt.Client | None
    _task_group: asyncio.TaskGroup | None
    _serial_sender: Callable[[int, Any, int | None], Awaitable[bool | bytes | ProtobufMessage]] | None
    handshake: SerialHandshakeManager
    _storage_lock: asyncio.Lock
    _mcu_read_lock: asyncio.Lock
    _pending_mcu_read: _PendingMcuRead | None
    _process_slots: asyncio.Semaphore
    _mqtt_publish_lock: asyncio.Lock
    _mqtt_spool: DbmDeque | None
    mcu_registry: dict[int, McuHandler]
    _topic_aliases: dict[str, int]
    _next_alias_id: int

    def __init__(self, config: RuntimeConfig, state: RuntimeState, serial: SerialTransport) -> None:
        self.config, self.state, self.serial = config, state, serial
        self._mqtt_client, self._task_group, self._serial_sender = None, None, None

        from .handshake import SerialHandshakeManager, derive_serial_timing

        self.handshake = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=derive_serial_timing(config),
            send_frame=serial.send_raw,
            enqueue_mqtt=self.enqueue_mqtt,
            acknowledge_frame=serial.acknowledge,
            logger_=logger,
        )

        self._storage_lock, self._mcu_read_lock, self._pending_mcu_read = asyncio.Lock(), asyncio.Lock(), None
        self._process_slots = asyncio.Semaphore(int(state.process_max_concurrent))
        self._mqtt_publish_lock = asyncio.Lock()
        self._mqtt_spool = None
        if self.config.mqtt_spool_dir:
            self._mqtt_spool = DbmDeque(
                path=str(Path(self.config.mqtt_spool_dir) / "spool.db"), maxlen=self.state.mqtt_queue_limit
            )
        self._topic_aliases = {}
        self._next_alias_id = 1

        # [SIL-2] O(1) MCU Dispatch Registry
        self.mcu_registry: dict[int, McuHandler] = self._setup_mcu_registry(serial)
        for s in Status:
            if s != Status.ACK:
                self.mcu_registry[s.value] = self._make_status_handler(s)

    def _setup_mcu_registry(self, serial: SerialTransport) -> dict[int, McuHandler]:
        return {
            Command.CMD_XOFF.value: lambda _seq, _payload: self._handle_mcu_xoff(),
            Command.CMD_XON.value: lambda _seq, _payload: self._handle_mcu_xon(),
            Command.CMD_CONSOLE_WRITE.value: lambda _seq, p: self._on_mcu_console_write(cast(pb.ConsoleWrite, p)),
            Command.CMD_DATASTORE_PUT.value: lambda _seq, p: self._on_mcu_datastore_put(cast(pb.DatastorePut, p)),
            Command.CMD_DATASTORE_GET.value: lambda _seq, p: self._on_mcu_datastore_get(cast(pb.DatastoreGet, p)),
            Command.CMD_MAILBOX_PUSH.value: lambda _seq, p: self._on_mcu_mailbox_push(cast(pb.MailboxPush, p)),
            Command.CMD_MAILBOX_AVAILABLE.value: lambda seq, _payload: self._on_mcu_mailbox_available(seq),
            Command.CMD_MAILBOX_READ.value: lambda seq, _payload: self._on_mcu_mailbox_read(seq),
            Command.CMD_MAILBOX_PROCESSED.value: lambda _seq, p: self._on_mcu_mailbox_processed(
                cast(pb.MailboxProcessed, p)
            ),
            Command.CMD_FILE_WRITE.value: lambda _seq, p: self._on_mcu_file_write(cast(pb.FileWrite, p)),
            Command.CMD_FILE_READ.value: lambda _seq, p: self._on_mcu_file_read(cast(pb.FileRead, p)),
            Command.CMD_FILE_REMOVE.value: lambda _seq, p: self._on_mcu_file_remove(cast(pb.FileRemove, p)),
            Command.CMD_FILE_READ_RESP.value: lambda _seq, p: self._on_mcu_file_read_resp(cast(pb.FileReadResponse, p)),
            Command.CMD_PROCESS_RUN_ASYNC.value: lambda _seq, p: self._on_mcu_process_run(cast(pb.ProcessRunAsync, p)),
            Command.CMD_PROCESS_POLL.value: lambda _seq, p: self._on_mcu_process_poll(cast(pb.ProcessPoll, p)),
            Command.CMD_PROCESS_KILL.value: lambda _seq, p: self._stop_process(cast(pb.ProcessKill, p).pid),
            Command.CMD_DIGITAL_READ.value: lambda _seq, _payload: serial.send(
                Status.NOT_IMPLEMENTED.value,
                pb.GenericResponse(message="linux_originates_digital_read_requests"),
            ),
            Command.CMD_ANALOG_READ.value: lambda _seq, _payload: serial.send(
                Status.NOT_IMPLEMENTED.value,
                pb.GenericResponse(message="linux_originates_analog_read_requests"),
            ),
            Command.CMD_DIGITAL_READ_RESP.value: lambda _seq, p: self._on_pin_resp(
                p, Topic.DIGITAL, self.state.pending_digital_reads
            ),
            Command.CMD_ANALOG_READ_RESP.value: lambda _seq, p: self._on_pin_resp(
                p, Topic.ANALOG, self.state.pending_analog_reads
            ),
            Command.CMD_SPI_TRANSFER_RESP.value: lambda _seq, p: self._on_mcu_spi_resp(cast(pb.SpiTransferResponse, p)),
            Command.CMD_GET_CAPABILITIES_RESP.value: self.handshake.handle_capabilities_resp,
            Command.CMD_LINK_SYNC_RESP.value: self.handshake.handle_link_sync_resp,
            Command.CMD_LINK_RESET_RESP.value: self.handshake.handle_link_reset_resp,
            Status.ACK.value: self._on_mcu_ack,
        }

    def _make_status_handler(self, status: Status) -> McuHandler:
        async def _handler(seq: int, payload: bytes | ProtobufMessage) -> bool | ProtobufMessage | None:
            await self._handle_mcu_status(seq, status, payload)
            return True

        return _handler

    # --- External Interface ---

    def register_serial_sender(
        self, sender: Callable[[int, Any, int | None], Awaitable[bool | bytes | ProtobufMessage]]
    ) -> None:
        self._serial_sender = sender

    def set_mqtt_client(self, client: aiomqtt.Client | None) -> None:
        self._mqtt_client = client
        self._topic_aliases.clear()
        self._next_alias_id = 1

    async def enqueue_mqtt(self, message: QueuedPublish, *, reply_context: Message | None = None) -> None:
        resolved_message = self._resolve_reply_message(message, reply_context)
        self.state.mqtt_publish_queue.put_nowait(resolved_message)
        try:
            async with self._mqtt_publish_lock:
                await self._flush_mqtt_spool_locked()
                if await self._publish_mqtt_message(resolved_message):
                    return
                if await self._spool_mqtt_message_locked(resolved_message):
                    return
                self._record_mqtt_drop(resolved_message.topic_name)
        finally:
            try:
                self.state.mqtt_publish_queue.get_nowait()
            except asyncio.QueueEmpty:
                logger.debug("MQTT publish queue already empty during pop")

    async def flush_mqtt_spool(self) -> None:
        async with self._mqtt_publish_lock:
            await self._flush_mqtt_spool_locked()

    def _resolve_reply_message(self, message: QueuedPublish, reply_context: Message | Any | None) -> QueuedPublish:
        topic = message.topic_name
        correlation_data = message.correlation_data
        user_properties = message.user_properties

        r_props = getattr(reply_context, "properties", None)
        if reply_context is not None and r_props is not None:
            if rt := getattr(r_props, "ResponseTopic", None):
                topic = rt
            if cd := getattr(r_props, "CorrelationData", None):
                correlation_data = cd
            if req_topic := getattr(reply_context, "topic", None):
                user_properties = (*user_properties, ("bridge-request-topic", str(req_topic)))

        return msgspec.structs.replace(
            message,
            topic_name=topic,
            correlation_data=correlation_data,
            user_properties=user_properties,
        )

    def _record_mqtt_drop(self, topic_name: str) -> None:
        self.state.mqtt_drop_counts[topic_name] = self.state.mqtt_drop_counts.get(topic_name, 0) + 1
        self.state.mqtt_dropped_messages += 1
        self.state.metrics.mqtt_messages_dropped.inc()

    def _mqtt_spool_dir(self) -> Path:
        return Path(self.config.mqtt_spool_dir)

    def _mark_mqtt_spool_failure(self, reason: str) -> None:
        self.state.mqtt_spool_degraded = True
        self.state.mqtt_spool_failure_reason = reason

    def _mark_mqtt_spool_healthy(self, pending_count: int) -> None:
        self.state.mqtt_spool_degraded = False
        self.state.mqtt_spool_failure_reason = None
        self.state.mqtt_spool_pending_messages = pending_count

    async def _spool_mqtt_message_locked(self, message: QueuedPublish) -> bool:
        spool = self._mqtt_spool
        if spool is None:
            return False
        try:
            if self.state.mqtt_queue_limit > 0:
                spool_len = await asyncio.to_thread(len, spool)
                while spool_len >= self.state.mqtt_queue_limit:
                    try:
                        popleft_fn = spool.popleft
                        await asyncio.to_thread(popleft_fn)
                        self.state.mqtt_spool_dropped_limit += 1
                    except IndexError as exc:
                        logger.error("Spool popped while empty during limit check", error=str(exc))
                        break
                    except OSError as exc:
                        logger.error("Database error during spool popleft", error=str(exc))
                        break
                    spool_len = await asyncio.to_thread(len, spool)

                if self.state.mqtt_spool_dropped_limit > 0:
                    self.state.mqtt_spool_trim_events += 1
                    self.state.mqtt_spool_last_trim_unix = time.time()

            encoded = msgspec.msgpack.encode(message)
            append_fn = spool.append
            await asyncio.to_thread(append_fn, encoded)

            pending_count = await asyncio.to_thread(len, spool)
            self._mark_mqtt_spool_healthy(pending_count)
            return True
        except OSError as exc:
            self._mark_mqtt_spool_failure(str(exc))
            return False
        except msgspec.MsgspecError as exc:
            logger.error("Serialization failure for spool message", error=str(exc))
            return False

    async def _flush_mqtt_spool_locked(self) -> None:
        spool = self._mqtt_spool
        if not self._mqtt_client or spool is None:
            return

        try:
            spool_len = await asyncio.to_thread(len, spool)
        except OSError as exc:
            self._mark_mqtt_spool_failure(str(exc))
            return

        while spool_len > 0:
            try:

                def peek_fn() -> bytes:
                    return spool[0]

                encoded = peek_fn()
                queued = msgspec.msgpack.decode(encoded, type=QueuedPublish)
            except IndexError as exc:
                logger.warning("Spool is empty during peek", error=str(exc))
                break
            except (ValueError, TypeError, msgspec.MsgspecError) as exc:
                logger.warning("Dropping corrupt MQTT spool entry", error=str(exc))
                try:
                    spool_len = await asyncio.to_thread(len, spool)
                    if spool_len > 0:
                        popleft_fn = spool.popleft
                        await asyncio.to_thread(popleft_fn)
                except IndexError as pop_exc:
                    logger.error("Failed to pop corrupt entry", error=str(pop_exc))
                    break
                except OSError as pop_exc:
                    logger.error("Database error while popping corrupt entry", error=str(pop_exc))
                    break
                self.state.mqtt_spool_corrupt_dropped += 1
                try:
                    spool_len = await asyncio.to_thread(len, spool)
                except OSError:
                    break
                continue
            except OSError as exc:
                self._mark_mqtt_spool_failure(str(exc))
                break

            if not await self._publish_mqtt_message(queued):
                break

            try:
                popleft_fn = spool.popleft
                await asyncio.to_thread(popleft_fn)
            except IndexError as exc:
                logger.warning("Spool was empty during popleft", error=str(exc))
                break
            except OSError as exc:
                self._mark_mqtt_spool_failure(str(exc))
                break

            try:
                spool_len = await asyncio.to_thread(len, spool)
            except OSError as exc:
                self._mark_mqtt_spool_failure(str(exc))
                break

        try:
            pending_count = await asyncio.to_thread(len, spool)
            if not self.state.mqtt_spool_degraded:
                self._mark_mqtt_spool_healthy(pending_count)
            else:
                self.state.mqtt_spool_pending_messages = pending_count
        except OSError as exc:
            self._mark_mqtt_spool_failure(str(exc))

    async def _publish_mqtt_message(self, message: QueuedPublish) -> bool:
        if not self._mqtt_client:
            return False

        topic_alias_max = 0
        client_obj = getattr(self._mqtt_client, "_client", None)
        if client_obj is not None:
            connack_props = getattr(client_obj, "_connack_properties", None)
            if connack_props is not None:
                val = getattr(connack_props, "TopicAliasMaximum", 0)
                if type(val) is int:
                    topic_alias_max = val

        pub_message = message
        if topic_alias_max > 0:
            if message.topic_name in self._topic_aliases:
                alias_id = self._topic_aliases[message.topic_name]
                pub_message = msgspec.structs.replace(
                    message,
                    topic_name="",
                    topic_alias=alias_id,
                )
            elif message.topic_name:
                if self._next_alias_id <= topic_alias_max:
                    alias_id = self._next_alias_id
                    self._topic_aliases[message.topic_name] = alias_id
                    self._next_alias_id += 1
                    pub_message = msgspec.structs.replace(
                        message,
                        topic_alias=alias_id,
                    )

        try:
            await self._mqtt_client.publish(
                pub_message.topic_name,
                pub_message.payload,
                qos=int(pub_message.qos),
                retain=pub_message.retain,
                properties=structures.build_mqtt_properties(pub_message),
            )
            self.state.metrics.mqtt_messages_published.inc()
            return True
        except (aiomqtt.MqttError, OSError, RuntimeError) as exc:
            logger.warning("MQTT publish failure: %s", exc)
            return False

    # --- Lifecycle ---

    async def __aenter__(self) -> BridgeService:
        self._task_group = asyncio.TaskGroup()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(self, et: Any, ev: Any, tb: Any) -> None:
        try:
            if self._task_group:
                await self._task_group.__aexit__(et, ev, tb)
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        """Explicitly cleanup and close the spool cache database connection (SIL 2)."""
        self.serial = None
        spool = self._mqtt_spool
        if spool is not None:
            try:
                spool.close()
            except (AttributeError, OSError, RuntimeError) as e:
                logger.debug("Spool cache close error during cleanup", error=e)
            self._mqtt_spool = None

        if self.state:
            self.state.cleanup()

    def __del__(self) -> None:
        """Last-resort cleanup for spool database cache connections."""
        self.cleanup()

    async def on_serial_connected(self) -> None:
        self.state.mark_transport_connected()
        await self.handshake.synchronize()
        if self.state.is_synchronized:
            await self._request_mcu_version()
            await self._flush_console_queue()

    async def on_serial_disconnected(self) -> None:
        self.state.mark_transport_disconnected()
        for q in (self.state.pending_digital_reads, self.state.pending_analog_reads):
            q.clear()
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        self.handshake.clear_handshake_expectations()
        serial = self.serial
        if serial:
            await serial.reset()

    # --- Dispatchers ---

    async def handle_mcu_frame(self, command_id: int, sequence_id: int, payload: bytes | ProtobufMessage) -> None:
        serial = self.serial
        if not serial:
            return
        if not (self.state.is_synchronized or command_id in _STATUS_VALUES or command_id in _PRE_SYNC_ALLOWED_COMMANDS):
            return

        if handler := self.mcu_registry.get(command_id):
            p = payload
            if not isinstance(p, ProtobufMessage) and command_id in _COMMAND_TO_PB:
                msg_cls = _COMMAND_TO_PB[command_id]
                p = msg_cls()
                p.ParseFromString(cast(bytes, payload))

            if await handler(sequence_id, p) is not False and command_id not in _STATUS_VALUES:
                await serial.acknowledge(command_id, sequence_id)
        elif response_to_request(command_id) is None:
            self.state.metrics.unknown_command_count.inc()
            await serial.send(Status.NOT_IMPLEMENTED.value, b"")

    async def handle_mqtt_message(self, inbound: Message) -> None:
        if route := parse_topic(self.state.mqtt_topic_prefix, str(inbound.topic)):
            if route.topic != Topic.SYSTEM:
                try:
                    async with asyncio.timeout(30.0):
                        await self.state.link_sync_event.wait()
                except asyncio.TimeoutError:
                    logger.warning("Timed out waiting for MCU link synchronization", topic=str(inbound.topic))
            action = self._deduce_action(route)
            if action and not (
                self.state.topic_authorization.allows(
                    route.topic.value if isinstance(route.topic, Topic) else route.topic, action
                )
                if self.state.topic_authorization
                else False
            ):
                await self._reject_mqtt(inbound, route.topic, action)
                return

            # Unified MQTT Dispatch
            match route.topic:
                case Topic.CONSOLE:
                    await self._handle_mqtt_console(inbound)
                case Topic.DATASTORE:
                    await self._handle_mqtt_datastore(route, inbound)
                case Topic.MAILBOX:
                    await self._handle_mqtt_mailbox(route, inbound)
                case Topic.FILE:
                    await self._handle_mqtt_file(route, inbound)
                case Topic.SHELL:
                    await self._handle_mqtt_shell(route, inbound)
                case Topic.SPI:
                    await self._handle_mqtt_spi(route, inbound)
                case Topic.DIGITAL | Topic.ANALOG:
                    await self._handle_mqtt_pin(route, inbound)
                case Topic.SYSTEM:
                    await self._handle_mqtt_system(route, inbound)
                case _:
                    pass

    # --- Business Logic Implementation ---

    async def _handle_mcu_xon(self) -> None:
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        await self._flush_console_queue()

    async def _handle_mcu_xoff(self) -> None:
        self.state.mcu_is_paused = True
        self.state.serial_tx_allowed.clear()

    async def _on_mcu_console_write(self, p: pb.ConsoleWrite) -> None:
        if p.data:
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_path(self.state.mqtt_topic_prefix, Topic.CONSOLE, ConsoleAction.OUT),
                    p.data,
                    message_expiry_interval=MQTT_EXPIRY_CONSOLE,
                )
            )

    async def _on_mcu_datastore_put(self, p: pb.DatastorePut) -> bool:
        if self.state.datastore_cache is not None:
            self.state.datastore_cache[p.key] = bytes(p.value)
        await self._publish_datastore_value(p.key, bytes(p.value))
        return True

    async def _on_mcu_datastore_get(self, p: pb.DatastoreGet) -> bool:
        serial = self.serial
        if not serial:
            return False
        cache = cast(Any, self.state.datastore_cache)
        val = msgspec.convert(cache.get(p.key, b"") if cache else b"", bytes)
        res = await serial.send(
            Command.CMD_DATASTORE_GET_RESP.value,
            pb.DatastoreGetResponse(value=val[:255]),
        )
        return bool(res)

    async def _on_mcu_mailbox_push(self, p: pb.MailboxPush) -> bool:
        self.state.mailbox_incoming_queue.append(bytes(p.data))
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_path(self.state.mqtt_topic_prefix, Topic.MAILBOX, MailboxAction.INCOMING), bytes(p.data)
            )
        )
        return True

    async def _on_mcu_mailbox_available(self, seq: int) -> bool:
        serial = self.serial
        if not serial:
            return False
        res = await serial.send(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            pb.MailboxAvailableResponse(count=len(self.state.mailbox_queue)),
        )
        return bool(res)

    async def _on_mcu_mailbox_read(self, seq: int) -> bool:
        serial = self.serial
        if not serial:
            return False
        res = await serial.send(
            Command.CMD_MAILBOX_READ_RESP.value,
            pb.MailboxReadResponse(content=self.state.mailbox_queue.popleft() if self.state.mailbox_queue else b""),
        )
        return bool(res)

    async def _on_mcu_mailbox_processed(self, p: pb.MailboxProcessed) -> None:
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=topic_path(self.state.mqtt_topic_prefix, Topic.MAILBOX, MailboxAction.PROCESSED),
                payload=p.SerializeToString(),
                content_type=PROTOBUF_CONTENT_TYPE,
            )
        )

    async def _on_mcu_file_write(self, p: pb.FileWrite) -> bool:
        serial = self.serial
        if not serial:
            return False
        path = self._get_safe_path(p.path)
        if path and await self._write_with_quota(path, p.data):
            res = await serial.send(Status.OK.value, b"")
            return bool(res)
        res = await serial.send(Status.ERROR.value, pb.GenericResponse(message="Write failed"))
        return bool(res)

    async def _on_mcu_file_read(self, p: pb.FileRead) -> None:
        serial = self.serial
        if not serial:
            return
        path = self._get_safe_path(p.path)
        if path and await asyncio.to_thread(path.is_file):
            data = await asyncio.to_thread(path.read_bytes)
            if not data:
                await serial.send(Command.CMD_FILE_READ_RESP.value, pb.FileReadResponse(content=b""))
            else:
                chunk_size = protocol.MAX_PAYLOAD_SIZE - 3
                for i in range(0, len(data), chunk_size):
                    chunk = data[i : i + chunk_size]
                    await serial.send(
                        Command.CMD_FILE_READ_RESP.value,
                        pb.FileReadResponse(content=chunk),
                    )
            return
        await serial.send(Status.ERROR.value, pb.GenericResponse(message="Read failed"))

    async def _on_mcu_file_remove(self, p: pb.FileRemove) -> bool:
        serial = self.serial
        if not serial:
            return False
        path = self._get_safe_path(p.path)
        if path and await asyncio.to_thread(path.exists):
            await asyncio.to_thread(path.unlink)
            res = await serial.send(Status.OK.value, b"")
            return bool(res)
        res = await serial.send(Status.ERROR.value, pb.GenericResponse(message="Remove failed"))
        return bool(res)

    async def _on_mcu_file_read_resp(self, p: pb.FileReadResponse) -> bool:
        if not self._pending_mcu_read:
            return False
        if p.content:
            self._pending_mcu_read.chunks.append(p.content)
        elif not self._pending_mcu_read.future.done():
            self._pending_mcu_read.future.set_result(b"".join(self._pending_mcu_read.chunks))
        return True

    async def _on_mcu_process_run(self, p: pb.ProcessRunAsync) -> bool:
        serial = self.serial
        if not serial:
            return False
        if p.command and self.state.allowed_policy.is_allowed(p.command):
            pid = await self._run_process(p.command)
            if pid:
                res = await serial.send(
                    Command.CMD_PROCESS_RUN_ASYNC_RESP.value,
                    pb.ProcessRunAsyncResponse(pid=pid),
                )
                return bool(res)
        await serial.send(Status.ERROR.value, pb.GenericResponse(message="Exec failed"))
        return False

    async def _on_mcu_process_poll(self, p: pb.ProcessPoll) -> bool:
        serial = self.serial
        if not serial:
            return False
        batch = await self._poll_process(p.pid)
        res = await serial.send(
            Command.CMD_PROCESS_POLL_RESP.value,
            pb.ProcessPollResponse(
                status=batch.status_byte,
                exit_code=batch.exit_code,
                stdout_data=batch.stdout_chunk,
                stderr_data=batch.stderr_chunk,
            ),
        )
        return bool(res)

    async def _on_pin_resp(self, p: Any, tp: Topic, q: collections.deque[structures.PendingPinRequest]) -> None:
        req = q.popleft() if q else None
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_path(self.state.mqtt_topic_prefix, tp, str(req.pin) if req else "unknown", "value"),
                str(p.value).encode(),
                message_expiry_interval=MQTT_EXPIRY_PIN,
                user_properties=(("bridge-pin", str(req.pin) if req else "unknown"),),
            ),
            reply_context=req.reply_context if req else None,
        )

    async def _on_mcu_spi_resp(self, p: pb.SpiTransferResponse) -> None:
        await self.enqueue_mqtt(
            QueuedPublish(topic_path(self.state.mqtt_topic_prefix, Topic.SPI, "transfer", "resp"), p.data)
        )

    async def _on_mcu_ack(self, seq: int, payload: bytes | ProtobufMessage) -> None:
        try:
            if isinstance(payload, ProtobufMessage):
                p = cast(pb.AckPacket, payload)
            else:
                p = pb.AckPacket.FromString(payload)
            logger.debug("MCU > ACK for 0x%02X", p.command_id)
        except (ProtobufDecodeError, TypeError, ValueError) as e:
            logger.warning("Failed to decode MCU ACK packet", error=e)

    async def _handle_mcu_status(self, seq_id: int, status: Status, payload: bytes | ProtobufMessage) -> None:
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
        log_func = logger.warning if status not in {Status.OK, Status.ACK} else logger.debug
        log_func("MCU > %s: %s %s", status.name, status.description, text)
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS),
                structures.encode_structured_payload(
                    {
                        "status": status.value,
                        "name": status.name,
                        "description": status.description,
                        "message": text,
                    }
                ),
                content_type=PROTOBUF_CONTENT_TYPE,
                user_properties=(("bridge-status", status.name),),
            )
        )

    # --- MQTT Specific Handlers (Cleaned) ---

    async def _handle_mqtt_console(self, inbound: Message) -> None:
        if pl := msgspec.convert(inbound.payload, bytes):
            self.state.console_to_mcu_queue.append(pl)
            await self._flush_console_queue()

    async def _handle_mqtt_datastore(self, route: TopicRoute, inbound: Message) -> None:
        key_parts = list(route.remainder)
        if key_parts and key_parts[-1] in ("request", "response"):
            key_parts.pop()
        key = "/".join(key_parts)
        pl = msgspec.convert(inbound.payload, bytes)
        if not key:
            return
        if route.identifier == DatastoreAction.PUT:
            if len(key.encode()) <= 255 and len(pl) <= 255:
                if self.state.datastore_cache is not None:
                    self.state.datastore_cache[key] = pl
                await self._publish_datastore_value(key, pl, reply_context=inbound)
        elif route.identifier == DatastoreAction.GET:
            cache = cast(Any, self.state.datastore_cache)
            val = cache.get(key) if cache else None
            if val is not None:
                await self._publish_datastore_value(key, msgspec.convert(val, bytes), reply_context=inbound)
            elif route.remainder and route.remainder[-1] == "request":
                await self._publish_datastore_value(key, b"", reply_context=inbound, error="datastore-miss")

    async def _handle_mqtt_mailbox(self, route: TopicRoute, inbound: Message) -> None:
        serial = self.serial
        if not serial:
            return
        pl = msgspec.convert(inbound.payload, bytes)
        if route.identifier == MailboxAction.WRITE:
            self.state.mailbox_queue.append(pl)
            await serial.send(Command.CMD_MAILBOX_PUSH.value, pb.MailboxPush(data=pl))
        elif route.identifier == MailboxAction.READ:
            data = self.state.mailbox_incoming_queue.popleft() if self.state.mailbox_incoming_queue else b""
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_path(
                        self.state.mqtt_topic_prefix, Topic.MAILBOX, MailboxAction.READ, protocol.MQTT_SUFFIX_RESPONSE
                    ),
                    data,
                ),
                reply_context=inbound,
            )

    async def _handle_mqtt_file(self, route: TopicRoute, inbound: Message) -> None:
        serial = self.serial
        if not serial:
            return
        act, target = route.action, "/".join(route.remainder)
        if not (act and target):
            return
        if target.startswith("mcu/"):
            if act == FileAction.READ:
                await self._handle_mqtt_file_mcu_read(inbound, target)
            elif act == FileAction.WRITE:
                await serial.send(
                    Command.CMD_FILE_WRITE.value,
                    pb.FileWrite(path=target[4:], data=msgspec.convert(inbound.payload, bytes)),
                )
            elif act == FileAction.REMOVE:
                await serial.send(Command.CMD_FILE_REMOVE.value, pb.FileRemove(path=target[4:]))
        else:
            path = self._get_safe_path(target)
            if not path:
                return
            if act == FileAction.WRITE:
                if await self._write_with_quota(path, msgspec.convert(inbound.payload, bytes)):
                    await self.enqueue_mqtt(
                        QueuedPublish(
                            topic_path(self.state.mqtt_topic_prefix, Topic.FILE, FileAction.READ, target),
                            msgspec.convert(inbound.payload, bytes),
                        ),
                        reply_context=inbound,
                    )
            elif act == FileAction.READ and await asyncio.to_thread(path.is_file):
                if not inbound.topic.value.endswith(protocol.MQTT_SUFFIX_RESPONSE):
                    await self.enqueue_mqtt(
                        QueuedPublish(
                            topic_path(
                                self.state.mqtt_topic_prefix,
                                Topic.FILE,
                                FileAction.READ,
                                target,
                                protocol.MQTT_SUFFIX_RESPONSE,
                            ),
                            await asyncio.to_thread(path.read_bytes),
                        ),
                        reply_context=inbound,
                    )
            elif act == FileAction.REMOVE and await asyncio.to_thread(path.exists):
                await asyncio.to_thread(path.unlink)

    async def _handle_mqtt_file_mcu_read(self, ctx: Message, target: str) -> None:
        serial = self.serial
        if not serial:
            return
        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.FILE,
            FileAction.READ,
            protocol.MQTT_SUFFIX_RESPONSE,
            target,
        )
        async with self._mcu_read_lock:
            self._pending_mcu_read = _PendingMcuRead(target, asyncio.get_running_loop().create_future())
            if not await serial.send_raw(
                Command.CMD_FILE_READ.value,
                pb.FileRead(path=target[4:]),
            ):
                logger.warning("MCU file read dispatch failed", target=target)
                await self.enqueue_mqtt(
                    QueuedPublish(
                        response_topic,
                        b"error:mcu_file_read_dispatch_failed",
                        user_properties=(("bridge-error", "mcu-file-read-dispatch-failed"),),
                    ),
                    reply_context=ctx,
                )
                self._pending_mcu_read = None
                return
            try:
                timeout_seconds = max(0.1, self.state.serial_response_timeout_ms / 1000.0)
                res = await asyncio.wait_for(self._pending_mcu_read.future, timeout_seconds)
                await self.enqueue_mqtt(
                    QueuedPublish(
                        response_topic,
                        res,
                    ),
                    reply_context=ctx,
                )
            except TimeoutError:
                logger.warning("Timed out waiting for MCU file read response", target=target)
                await self.enqueue_mqtt(
                    QueuedPublish(
                        response_topic,
                        b"error:mcu_file_read_timeout",
                        user_properties=(("bridge-error", "mcu-file-read-timeout"),),
                    ),
                    reply_context=ctx,
                )
            finally:
                self._pending_mcu_read = None

    async def _handle_mqtt_shell(self, route: TopicRoute, inbound: Message) -> None:
        act = route.segments[0] if route.segments else None
        pl = msgspec.convert(inbound.payload, bytes)
        if act == ShellAction.RUN_ASYNC:
            try:
                properties = getattr(inbound, "properties", None)
                content_type = getattr(properties, "ContentType", None) if properties else None
                if content_type == PROTOBUF_CONTENT_TYPE or pl.startswith(b"\x0a"):
                    cmd = pb.ProcessRunAsync.FromString(pl).command
                else:
                    cmd = pl.decode().strip()
                pid = await self._run_process(cmd)
            except (ProtobufDecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
                logger.warning("MQTT shell run_async rejected", error=str(exc))
                payload = pb.ProcessRunAsyncResponse(pid=0).SerializeToString()
            else:
                payload = pb.ProcessRunAsyncResponse(pid=pid).SerializeToString()
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_path(
                        self.state.mqtt_topic_prefix,
                        Topic.SHELL,
                        ShellAction.RUN_ASYNC,
                        protocol.MQTT_SUFFIX_RESPONSE,
                    ),
                    payload,
                    content_type=PROTOBUF_CONTENT_TYPE,
                ),
                reply_context=inbound,
            )
        elif act in (ShellAction.POLL, ShellAction.KILL) and len(route.segments) == 2:
            pid = int(route.segments[1])
            if act == ShellAction.POLL:
                batch = await self._poll_process(pid)
                await self.enqueue_mqtt(
                    QueuedPublish(
                        topic_path(
                            self.state.mqtt_topic_prefix,
                            Topic.SHELL,
                            ShellAction.POLL,
                            str(pid),
                            protocol.MQTT_SUFFIX_RESPONSE,
                        ),
                        pb.ProcessPollResponse(
                            status=batch.status_byte,
                            exit_code=batch.exit_code,
                            stdout_data=batch.stdout_chunk,
                            stderr_data=batch.stderr_chunk,
                            finished=batch.finished,
                            stdout_truncated=batch.stdout_truncated,
                            stderr_truncated=batch.stderr_truncated,
                        ).SerializeToString(),
                        content_type=PROTOBUF_CONTENT_TYPE,
                    ),
                    reply_context=inbound,
                )
            else:
                await self._stop_process(pid)

    async def _handle_mqtt_spi(self, route: TopicRoute, inbound: Message) -> None:
        serial = self.serial
        if not serial:
            return
        match route.identifier:
            case SpiAction.BEGIN:
                await serial.send(Command.CMD_SPI_BEGIN.value, b"")
            case SpiAction.END:
                await serial.send(Command.CMD_SPI_END.value, b"")
            case SpiAction.CONFIG:
                try:
                    p = pb.SpiConfig.FromString(inbound.payload)
                    await serial.send(Command.CMD_SPI_SET_CONFIG.value, p)
                except (ProtobufDecodeError, TypeError, ValueError) as exc:
                    logger.error("SPI config error: %s", exc)
            case SpiAction.TRANSFER:
                if inbound.payload:
                    res = await serial.send(
                        Command.CMD_SPI_TRANSFER.value,
                        pb.SpiTransfer(data=bytes(inbound.payload)),
                    )
                    if isinstance(res, bytes):
                        await self.enqueue_mqtt(
                            QueuedPublish(
                                topic_path(
                                    self.state.mqtt_topic_prefix,
                                    Topic.SPI,
                                    SpiAction.TRANSFER,
                                    protocol.MQTT_SUFFIX_RESPONSE,
                                ),
                                pb.SpiTransferResponse.FromString(res).data,
                            ),
                            reply_context=inbound,
                        )
            case _:
                return

    async def _handle_mqtt_pin(self, route: TopicRoute, inbound: Message) -> None:
        serial = self.serial
        if not serial:
            return
        pin = self._parse_pin(route.segments[0])
        if pin < 0:
            return
        pl = msgspec.convert(inbound.payload, bytes).decode()
        if len(route.segments) == 2:
            if route.segments[1] == PinAction.MODE:
                await serial.send(Command.CMD_SET_PIN_MODE.value, pb.PinMode(pin=pin, mode=cast(Any, int(pl))))
            elif route.segments[1] == PinAction.READ:
                cmd = Command.CMD_DIGITAL_READ if route.topic == Topic.DIGITAL else Command.CMD_ANALOG_READ
                q = (
                    self.state.pending_digital_reads
                    if cmd == Command.CMD_DIGITAL_READ
                    else self.state.pending_analog_reads
                )
                if len(q) < self.state.pending_pin_request_limit:
                    q.append(structures.PendingPinRequest(pin=pin, reply_context=inbound))
                    await serial.send(cmd.value, pb.PinRead(pin=pin))
                else:
                    await self.enqueue_mqtt(
                        QueuedPublish(
                            topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS),
                            structures.encode_structured_payload(
                                {
                                    "status": "error",
                                    "topic": route.topic.value,
                                    "pin": pin,
                                    "action": PinAction.READ,
                                    "reason": "pending-pin-overflow",
                                }
                            ),
                            content_type=PROTOBUF_CONTENT_TYPE,
                            user_properties=(
                                ("bridge-error", "pending-pin-overflow"),
                                ("bridge-pin", str(pin)),
                            ),
                        ),
                        reply_context=inbound,
                    )
        else:
            cmd = Command.CMD_DIGITAL_WRITE if route.topic == Topic.DIGITAL else Command.CMD_ANALOG_WRITE
            await serial.send(cmd.value, pb.DigitalWrite(pin=pin, value=int(pl) if pl.isdigit() else 0))

    async def _handle_mqtt_system(self, route: TopicRoute, inbound: Message) -> None:
        serial = self.serial
        if not serial:
            return
        match route.identifier:
            case SystemAction.BOOTLOADER:
                await serial.send(
                    Command.CMD_ENTER_BOOTLOADER.value,
                    pb.EnterBootloader(magic=protocol.BOOTLOADER_MAGIC),
                )
            case SystemAction.FREE_MEMORY if "get" in route.segments:
                pl = await serial.send(Command.CMD_GET_FREE_MEMORY.value, b"")
                if isinstance(pl, bytes):
                    await self.enqueue_mqtt(
                        QueuedPublish(
                            topic_path(
                                self.state.mqtt_topic_prefix, Topic.SYSTEM, SystemAction.FREE_MEMORY, SystemAction.VALUE
                            ),
                            str(pb.FreeMemoryResponse.FromString(pl).value).encode(),
                        ),
                        reply_context=inbound,
                    )
            case SystemAction.VERSION if "get" in route.segments:
                await self._request_mcu_version(inbound)
            case SystemAction.BRIDGE:
                flavor = route.segments[1] if len(route.segments) > 1 else "summary"
                snap = (
                    self.state.build_handshake_snapshot()
                    if flavor == "handshake"
                    else self.state.build_bridge_snapshot()
                )
                await self.enqueue_mqtt(
                    QueuedPublish(
                        topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, "bridge", flavor, "value"),
                        structures.encode_structured_payload(snap),
                        content_type=PROTOBUF_CONTENT_TYPE,
                    ),
                    reply_context=inbound,
                )
            case _:
                return

    # --- Low-level Helpers ---

    async def _request_mcu_version(self, inbound: Message | None = None) -> bool:
        serial = self.serial
        if not serial:
            return False
        pl = await serial.send(Command.CMD_GET_VERSION.value, b"")
        if isinstance(pl, bytes):
            p = pb.VersionResponse.FromString(pl)
            self.state.mcu_version = (p.major, p.minor, p.patch)
            await self._publish_version(self.state.mcu_version, inbound)
            return True
        return False

    async def _publish_version(self, v: tuple[int, int, int], ctx: Message | None) -> None:
        pl, tp = f"{v[0]}.{v[1]}.{v[2]}".encode(), topic_path(
            self.state.mqtt_topic_prefix, Topic.SYSTEM, SystemAction.VERSION, SystemAction.VALUE
        )
        await self.enqueue_mqtt(QueuedPublish(tp, pl, message_expiry_interval=MQTT_EXPIRY_DATASTORE), reply_context=ctx)

    async def _flush_console_queue(self) -> None:
        serial = self.serial
        if not serial:
            return
        while self.state.console_to_mcu_queue and not self.state.mcu_is_paused:
            buf = self.state.console_to_mcu_queue.popleft()
            chunk_size = protocol.MAX_PAYLOAD_SIZE
            for i in range(0, len(buf), chunk_size):
                chunk = buf[i : i + chunk_size]
                if not await serial.send(Command.CMD_CONSOLE_WRITE.value, pb.ConsoleWrite(data=chunk)):
                    self.state.console_to_mcu_queue.appendleft(buf)
                    return

    async def _run_process(self, command: str) -> int:
        if not self.state.allowed_policy.is_allowed(command):
            return 0
        await self._process_slots.acquire()
        try:
            p = await asyncio.create_subprocess_exec(
                *shlex.split(command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            pid = p.pid & 0xFFFF
            async with self.state.process_lock:
                self.state.running_processes[pid] = ProcessContext(p)
            asyncio.create_task(self._monitor_process(pid))
            return pid
        except OSError:
            self._process_slots.release()
            return 0

    async def _monitor_process(self, pid: int) -> None:
        try:
            async with self.state.process_lock:
                ctx = self.state.running_processes.get(pid)
            if ctx:
                try:
                    ctx.exit_code = await asyncio.wait_for(ctx.handle.wait(), float(self.state.process_timeout))
                except TimeoutError:
                    ctx.exit_code = await self._terminate_process(pid, ctx, grace_period=0.5)
        finally:
            self._finalize_process(pid)

    async def _poll_process(self, pid: int) -> ProcessOutputBatch:
        async with self.state.process_lock:
            ctx = self.state.running_processes.get(pid)
            if not ctx:
                return ProcessOutputBatch(Status.ERROR.value, 1, b"", b"", True, False, False)
            async with ctx.io_lock:

                async def _rd(s: asyncio.StreamReader | None) -> tuple[bytes, bool]:
                    if not s or s.at_eof():
                        return b"", False
                    try:
                        return await asyncio.wait_for(s.read(protocol.MAX_PAYLOAD_SIZE - 32), 0.01), not s.at_eof()
                    except TimeoutError:
                        return b"", True

                o, to = await _rd(ctx.handle.stdout)
                e, te = await _rd(ctx.handle.stderr)
                fin = ctx.handle.returncode is not None
                if (
                    fin
                    and (ctx.handle.stdout is None or ctx.handle.stdout.at_eof())
                    and (ctx.handle.stderr is None or ctx.handle.stderr.at_eof())
                ):
                    self._finalize_process(pid)
                return ProcessOutputBatch(Status.OK.value, ctx.exit_code, o, e, fin, to, te)

    async def _stop_process(self, pid: int) -> bool:
        async with self.state.process_lock:
            ctx = self.state.running_processes.get(pid)
        if not ctx:
            return False
        try:
            ctx.exit_code = await self._terminate_process(pid, ctx, grace_period=0.5)
        except (OSError, ProcessLookupError) as exc:
            logger.warning("Process termination failed", pid=pid, error=str(exc))
        self._finalize_process(pid)
        return True

    async def _terminate_process(self, pid: int, ctx: ProcessContext, *, grace_period: float) -> int:
        if ctx.handle.returncode is not None:
            return int(ctx.handle.returncode)
        try:
            os.killpg(ctx.handle.pid, signal.SIGTERM)
        except ProcessLookupError:
            return int(ctx.handle.returncode or -1)

        try:
            return int(await asyncio.wait_for(ctx.handle.wait(), grace_period))
        except TimeoutError:
            logger.warning("Process exceeded graceful shutdown window; escalating to SIGKILL", pid=pid)

        os.killpg(ctx.handle.pid, signal.SIGKILL)
        try:
            return int(await asyncio.wait_for(ctx.handle.wait(), 0.5))
        except TimeoutError:
            return -1

    def _finalize_process(self, pid: int) -> None:
        if self.state.running_processes.pop(pid, None):
            self._process_slots.release()

    def _get_safe_path(self, p_str: str) -> Path | None:
        root = Path(self.config.file_system_root).resolve()
        p = root.joinpath(p_str.lstrip("/")).resolve()
        return p if p.is_relative_to(root) else None

    async def _write_with_quota(self, path: Path, data: bytes) -> bool:
        async with self._storage_lock:
            import shutil

            try:
                usage = await asyncio.to_thread(shutil.disk_usage, self.config.file_system_root)
                self.state.file_storage_bytes_used = usage.used
                if usage.free < len(data):
                    self.state.file_storage_limit_rejections += 1
                    return False
            except OSError as exc:
                logger.warning("Disk usage check failed", error=exc)
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, data)
            return True

    def _parse_pin(self, s: str) -> int:
        s = s.upper()
        return int(s[1:]) if s.startswith("A") and s[1:].isdigit() else (int(s) if s.isdigit() else -1)

    def _deduce_action(self, r: TopicRoute) -> str | None:
        if r.topic == Topic.SYSTEM:
            return None
        if r.topic in (Topic.DIGITAL, Topic.ANALOG):
            return "write" if len(r.segments) == 1 else (r.segments[1].lower() if len(r.segments) > 1 else None)
        return "in" if r.topic == Topic.CONSOLE and r.identifier == "in" else (r.identifier or None)

    async def _reject_mqtt(self, ctx: Message, tp: Topic | str, act: str) -> None:
        val = tp.value if isinstance(tp, Topic) else tp
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS),
                structures.encode_structured_payload({"status": "forbidden", "topic": val, "action": act}),
                content_type=PROTOBUF_CONTENT_TYPE,
                user_properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
            ),
            reply_context=ctx,
        )

    async def _publish_datastore_value(
        self, key: str, val: bytes, reply_context: Message | None = None, error: str | None = None
    ) -> None:
        tp = topic_path(
            self.state.mqtt_topic_prefix, Topic.DATASTORE, DatastoreAction.GET, *filter(None, key.split("/"))
        )
        props = (("bridge-datastore-key", key), ("bridge-error", error)) if error else (("bridge-datastore-key", key),)
        await self.enqueue_mqtt(
            QueuedPublish(tp, val, message_expiry_interval=MQTT_EXPIRY_DATASTORE, user_properties=props),
            reply_context=reply_context,
        )
