"""Flattened Service Core for MCU and MQTT orchestration. [SIL-2]"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
import shlex
from collections.abc import Coroutine, Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, Final

import msgspec
import structlog

import aiomqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

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
    AckPacket,
    AnalogReadResponsePacket,
    ConsoleWritePacket,
    DatastoreGetPacket,
    DatastoreGetResponsePacket,
    DatastorePutPacket,
    DigitalReadResponsePacket,
    DigitalWritePacket,
    EnterBootloaderPacket,
    FileReadPacket,
    FileReadResponsePacket,
    FileRemovePacket,
    FileWritePacket,
    FreeMemoryResponsePacket,
    MailboxAvailableResponsePacket,
    MailboxPushPacket,
    MailboxReadResponsePacket,
    PinModePacket,
    PinReadPacket,
    ProcessKillPacket,
    ProcessOutputBatch,
    ProcessPollPacket,
    ProcessRunAsyncPacket,
    ProcessRunAsyncResponsePacket,
    ProcessPollResponsePacket,
    QueuedPublish,
    ShellCommandPayload,
    SpiConfigPacket,
    SpiTransferResponsePacket,
    SpiTransferPacket,
    TopicRoute,
    VersionResponsePacket,
)
from ..protocol.topics import Topic, parse_topic, topic_path
from ..state.context import ProcessContext, RuntimeState

if TYPE_CHECKING:
    from ..transport.serial import SerialTransport

logger = structlog.get_logger("mcubridge.service")

McuHandler = Callable[[int, bytes], Coroutine[Any, Any, bool | None]]

_PRE_SYNC_ALLOWED_COMMANDS: Final = {
    Command.CMD_LINK_SYNC_RESP.value,
    Command.CMD_LINK_RESET_RESP.value,
}

_STATUS_VALUES: Final = {s.value for s in Status}


@dataclass
class _PendingMcuRead:
    identifier: str
    future: asyncio.Future[bytes]
    chunks: list[bytes] = field(default_factory=list)


class BridgeService:
    """Consolidated Service Façade Eradicating Component Wrappers. [SIL-2]"""

    def __init__(self, config: RuntimeConfig, state: RuntimeState, serial: SerialTransport) -> None:
        self.config, self.state, self.serial = config, state, serial
        self._mqtt_client, self._task_group, self._serial_sender = None, None, None

        from .handshake import SerialHandshakeManager, derive_serial_timing

        self.handshake = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=derive_serial_timing(config),
            send_frame=self.serial.send_raw,
            enqueue_mqtt=self.enqueue_mqtt,
            acknowledge_frame=self.serial.acknowledge,
            logger_=logger,
        )

        self._storage_lock, self._mcu_read_lock, self._pending_mcu_read = asyncio.Lock(), asyncio.Lock(), None
        self._process_slots = asyncio.Semaphore(int(state.process_max_concurrent))

        # [SIL-2] O(1) MCU Dispatch Registry
        self.mcu_registry: dict[int, McuHandler] = self._setup_mcu_registry()
        for s in Status:
            if s != Status.ACK:
                self.mcu_registry[s.value] = self._make_status_handler(s)

    def _setup_mcu_registry(self) -> dict[int, McuHandler]:
        return {
            Command.CMD_XOFF.value: lambda _, __: self._handle_mcu_xoff(),
            Command.CMD_XON.value: lambda _, __: self._handle_mcu_xon(),
            Command.CMD_CONSOLE_WRITE.value: self._gen_handler(ConsoleWritePacket, self._on_mcu_console_write),
            Command.CMD_DATASTORE_PUT.value: self._gen_handler(DatastorePutPacket, self._on_mcu_datastore_put),
            Command.CMD_DATASTORE_GET.value: self._gen_handler(DatastoreGetPacket, self._on_mcu_datastore_get),
            Command.CMD_MAILBOX_PUSH.value: self._gen_handler(MailboxPushPacket, self._on_mcu_mailbox_push),
            Command.CMD_MAILBOX_AVAILABLE.value: lambda seq, _: self._on_mcu_mailbox_available(seq),
            Command.CMD_MAILBOX_READ.value: lambda seq, _: self._on_mcu_mailbox_read(seq),
            Command.CMD_MAILBOX_PROCESSED.value: lambda _, p: self._on_mcu_mailbox_processed(p),
            Command.CMD_FILE_WRITE.value: self._gen_handler(FileWritePacket, self._on_mcu_file_write),
            Command.CMD_FILE_READ.value: self._gen_handler(FileReadPacket, self._on_mcu_file_read),
            Command.CMD_FILE_REMOVE.value: self._gen_handler(FileRemovePacket, self._on_mcu_file_remove),
            Command.CMD_FILE_READ_RESP.value: self._gen_handler(FileReadResponsePacket, self._on_mcu_file_read_resp),
            Command.CMD_PROCESS_RUN_ASYNC.value: self._gen_handler(ProcessRunAsyncPacket, self._on_mcu_process_run),
            Command.CMD_PROCESS_POLL.value: self._gen_handler(ProcessPollPacket, self._on_mcu_process_poll),
            Command.CMD_PROCESS_KILL.value: self._gen_handler(ProcessKillPacket, lambda p: self._stop_process(p.pid)),
            Command.CMD_DIGITAL_READ.value: lambda _, __: self.serial.send(
                Status.NOT_IMPLEMENTED.value, b"linux_gpio_read_not_available"
            ),
            Command.CMD_ANALOG_READ.value: lambda _, __: self.serial.send(
                Status.NOT_IMPLEMENTED.value, b"linux_adc_read_not_available"
            ),
            Command.CMD_DIGITAL_READ_RESP.value: self._gen_handler(
                DigitalReadResponsePacket,
                lambda p: self._on_pin_resp(p, Topic.DIGITAL, self.state.pending_digital_reads),
            ),
            Command.CMD_ANALOG_READ_RESP.value: self._gen_handler(
                AnalogReadResponsePacket, lambda p: self._on_pin_resp(p, Topic.ANALOG, self.state.pending_analog_reads)
            ),
            Command.CMD_GET_VERSION_RESP.value: self._gen_handler(VersionResponsePacket, self._on_mcu_version_resp),
            Command.CMD_GET_FREE_MEMORY_RESP.value: self._gen_handler(
                FreeMemoryResponsePacket, self._on_mcu_free_memory_resp
            ),
            Command.CMD_SPI_TRANSFER_RESP.value: self._gen_handler(SpiTransferResponsePacket, self._on_mcu_spi_resp),
            Command.CMD_GET_CAPABILITIES_RESP.value: self.handshake.handle_capabilities_resp,
            Command.CMD_LINK_SYNC_RESP.value: self.handshake.handle_link_sync_resp,
            Command.CMD_LINK_RESET_RESP.value: self.handshake.handle_link_reset_resp,
            Status.ACK.value: self._on_mcu_ack,
        }

    def _gen_handler(self, packet_type: type[Any], callback: Callable[[Any], Awaitable[Any]]) -> McuHandler:
        async def _handler(seq: int, payload: bytes) -> bool | None:
            try:
                p = msgspec.msgpack.decode(payload, type=packet_type)
                res = await callback(p)
                return True if res is None else res
            except (msgspec.MsgspecError, TypeError, ValueError) as exc:
                logger.error("MCU Payload decode error: %s", exc)
                return False

        return _handler

    def _make_status_handler(self, status: Status) -> McuHandler:
        async def _handler(seq: int, payload: bytes) -> bool | None:
            await self._handle_mcu_status(seq, status, payload)
            return True

        return _handler

    # --- External Interface ---

    def register_serial_sender(self, sender: Callable[[int, bytes], Awaitable[bool]]) -> None:
        self._serial_sender = sender

    def set_mqtt_client(self, client: aiomqtt.Client | None) -> None:
        self._mqtt_client = client

    async def enqueue_mqtt(self, message: QueuedPublish, *, reply_context: Message | None = None) -> None:
        if not self._mqtt_client:
            self.state.mqtt_dropped_messages += 1
            self.state.metrics.mqtt_messages_dropped.inc()
            return

        topic = message.topic_name
        props = Properties(PacketTypes.PUBLISH)

        if reply_context is not None and reply_context.properties:
            r_props = reply_context.properties
            if rt := getattr(r_props, "ResponseTopic", None):
                topic = rt
            if cd := getattr(r_props, "CorrelationData", None):
                props.CorrelationData = cd

            user_props = list(message.user_properties) + [("bridge-request-topic", str(reply_context.topic))]
            if user_props:
                props.UserProperty = user_props
        elif message.user_properties:
            props.UserProperty = list(message.user_properties)

        if message.content_type:
            props.ContentType = message.content_type
        if message.payload_format_indicator is not None:
            props.PayloadFormatIndicator = message.payload_format_indicator
        if message.message_expiry_interval:
            props.MessageExpiryInterval = message.message_expiry_interval
        if message.response_topic:
            props.ResponseTopic = message.response_topic
        if message.correlation_data and not getattr(props, "CorrelationData", None):
            props.CorrelationData = message.correlation_data

        try:
            await self._mqtt_client.publish(
                topic, message.payload, qos=int(message.qos), retain=message.retain, properties=props
            )
            self.state.metrics.mqtt_messages_published.inc()
        except (aiomqtt.MqttError, OSError, RuntimeError) as exc:
            logger.warning("MQTT publish failure: %s", exc)
            self.state.mqtt_dropped_messages += 1

    # --- Lifecycle ---

    async def __aenter__(self) -> BridgeService:
        self._task_group = asyncio.TaskGroup()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(self, et: Any, ev: Any, tb: Any) -> None:
        if self._task_group:
            await self._task_group.__aexit__(et, ev, tb)

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
        await self.serial.reset()

    # --- Dispatchers ---

    async def handle_mcu_frame(self, cmd_id: int, seq_id: int, payload: bytes) -> None:
        if not (self.state.is_synchronized or cmd_id in _STATUS_VALUES or cmd_id in _PRE_SYNC_ALLOWED_COMMANDS):
            return
        if handler := self.mcu_registry.get(cmd_id):
            if await handler(seq_id, payload) is not False and cmd_id not in _STATUS_VALUES:
                await self.serial.acknowledge(cmd_id, seq_id)
        elif response_to_request(cmd_id) is None:
            self.state.metrics.unknown_command_count.inc()
            await self.serial.send(Status.NOT_IMPLEMENTED.value, b"")

    async def handle_mqtt_message(self, inbound: Message) -> None:
        if route := parse_topic(self.state.mqtt_topic_prefix, str(inbound.topic)):
            if route.topic != Topic.SYSTEM:
                with contextlib.suppress(asyncio.TimeoutError):
                    async with asyncio.timeout(30.0):
                        await self.state.link_sync_event.wait()
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

    async def _on_mcu_console_write(self, p: ConsoleWritePacket) -> None:
        if p.data:
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_path(self.state.mqtt_topic_prefix, Topic.CONSOLE, ConsoleAction.OUT),
                    p.data,
                    message_expiry_interval=MQTT_EXPIRY_CONSOLE,
                )
            )

    async def _on_mcu_datastore_put(self, p: DatastorePutPacket) -> bool:
        if self.state.datastore_cache is not None:
            self.state.datastore_cache[p.key] = bytes(p.value)
        await self._publish_datastore_value(p.key, bytes(p.value))
        return True

    async def _on_mcu_datastore_get(self, p: DatastoreGetPacket) -> bool:
        cache = cast(Any, self.state.datastore_cache)
        val = msgspec.convert(cache.get(p.key, b"") if cache else b"", bytes)
        return await self.serial.send(
            Command.CMD_DATASTORE_GET_RESP.value,
            msgspec.msgpack.encode(DatastoreGetResponsePacket(value=msgspec.Raw(val[:255]))),
        )

    async def _on_mcu_mailbox_push(self, p: MailboxPushPacket) -> bool:
        self.state.mailbox_incoming_queue.append(bytes(p.data))
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_path(self.state.mqtt_topic_prefix, Topic.MAILBOX, MailboxAction.INCOMING), bytes(p.data)
            )
        )
        return True

    async def _on_mcu_mailbox_available(self, seq: int) -> bool:
        return await self.serial.send(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            msgspec.msgpack.encode(MailboxAvailableResponsePacket(count=len(self.state.mailbox_queue))),
        )

    async def _on_mcu_mailbox_read(self, seq: int) -> bool:
        return await self.serial.send(
            Command.CMD_MAILBOX_READ_RESP.value,
            msgspec.msgpack.encode(
                MailboxReadResponsePacket(
                    content=self.state.mailbox_queue.popleft() if self.state.mailbox_queue else b""
                )
            ),
        )

    async def _on_mcu_mailbox_processed(self, payload: bytes) -> None:
        await self.enqueue_mqtt(
            QueuedPublish(topic_path(self.state.mqtt_topic_prefix, Topic.MAILBOX, MailboxAction.PROCESSED), payload)
        )

    async def _on_mcu_file_write(self, p: FileWritePacket) -> bool:
        path = self._get_safe_path(p.path)
        if path and await self._write_with_quota(path, p.data):
            return await self.serial.send(Status.OK.value, b"")
        return await self.serial.send(Status.ERROR.value, b"Write failed")

    async def _on_mcu_file_read(self, p: FileReadPacket) -> None:
        path = self._get_safe_path(p.path)
        if path and path.is_file():
            data = await asyncio.to_thread(path.read_bytes)
            if not data:
                await self.serial.send(
                    Command.CMD_FILE_READ_RESP.value, msgspec.msgpack.encode(FileReadResponsePacket(content=b""))
                )
            else:
                for chunk in itertools.batched(data, protocol.MAX_PAYLOAD_SIZE - 3):
                    await self.serial.send(
                        Command.CMD_FILE_READ_RESP.value,
                        msgspec.msgpack.encode(FileReadResponsePacket(content=bytes(chunk))),
                    )
            return
        await self.serial.send(Status.ERROR.value, b"Read failed")

    async def _on_mcu_file_remove(self, p: FileRemovePacket) -> bool:
        path = self._get_safe_path(p.path)
        if path and path.exists():
            await asyncio.to_thread(path.unlink)
            return await self.serial.send(Status.OK.value, b"")
        return await self.serial.send(Status.ERROR.value, b"Remove failed")

    async def _on_mcu_file_read_resp(self, p: FileReadResponsePacket) -> bool:
        if not self._pending_mcu_read:
            return False
        if p.content:
            self._pending_mcu_read.chunks.append(p.content)
        elif not self._pending_mcu_read.future.done():
            self._pending_mcu_read.future.set_result(b"".join(self._pending_mcu_read.chunks))
        return True

    async def _on_mcu_process_run(self, p: ProcessRunAsyncPacket) -> bool:
        if p.command and self.state.allowed_policy.is_allowed(p.command):
            pid = await self._run_process(p.command)
            if pid:
                return await self.serial.send(
                    Command.CMD_PROCESS_RUN_ASYNC_RESP.value,
                    msgspec.msgpack.encode(ProcessRunAsyncResponsePacket(pid=pid)),
                )
        await self.serial.send(Status.ERROR.value, b"Exec failed")
        return False

    async def _on_mcu_process_poll(self, p: ProcessPollPacket) -> bool:
        batch = await self._poll_process(p.pid)
        return await self.serial.send(
            Command.CMD_PROCESS_POLL_RESP.value,
            msgspec.msgpack.encode(
                ProcessPollResponsePacket(
                    status=batch.status_byte,
                    exit_code=batch.exit_code,
                    stdout_data=batch.stdout_chunk,
                    stderr_data=batch.stderr_chunk,
                )
            ),
        )

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

    async def _on_mcu_spi_resp(self, p: SpiTransferResponsePacket) -> None:
        await self.enqueue_mqtt(
            QueuedPublish(topic_path(self.state.mqtt_topic_prefix, Topic.SPI, "transfer", "resp"), p.data)
        )

    async def _on_mcu_version_resp(self, p: VersionResponsePacket) -> None:
        self.state.mcu_version = (p.major, p.minor, p.patch)
        logger.debug("MCU > VERSION RESP: %d.%d.%d", p.major, p.minor, p.patch)

    async def _on_mcu_free_memory_resp(self, p: FreeMemoryResponsePacket) -> None:
        logger.debug("MCU > FREE MEMORY RESP: %d", p.value)

    async def _on_mcu_ack(self, seq: int, payload: bytes) -> None:
        with contextlib.suppress(msgspec.MsgspecError):
            p = msgspec.msgpack.decode(payload, type=AckPacket)
            logger.debug("MCU > ACK for 0x%02X", p.command_id)

    async def _handle_mcu_status(self, seq_id: int, status: Status, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="ignore") if payload else ""
        log_func = logger.warning if status not in {Status.OK, Status.ACK} else logger.debug
        log_func("MCU > %s: %s %s", status.name, status.description, text)
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS),
                msgspec.msgpack.encode(
                    {"status": status.value, "name": status.name, "description": status.description, "message": text}
                ),
                content_type="application/msgpack",
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
        pl = msgspec.convert(inbound.payload, bytes)
        if route.identifier == MailboxAction.WRITE:
            self.state.mailbox_queue.append(pl)
            await self.serial.send(Command.CMD_MAILBOX_PUSH.value, msgspec.msgpack.encode(MailboxPushPacket(data=pl)))
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
        act, target = route.action, "/".join(route.remainder)
        if not (act and target):
            return
        if target.startswith("mcu/"):
            if act == FileAction.READ:
                await self._handle_mqtt_file_mcu_read(inbound, target)
            elif act == FileAction.WRITE:
                await self.serial.send(
                    Command.CMD_FILE_WRITE.value,
                    msgspec.msgpack.encode(
                        FileWritePacket(path=target[4:], data=msgspec.convert(inbound.payload, bytes))
                    ),
                )
            elif act == FileAction.REMOVE:
                await self.serial.send(
                    Command.CMD_FILE_REMOVE.value, msgspec.msgpack.encode(FileRemovePacket(path=target[4:]))
                )
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
            elif act == FileAction.READ and path.is_file():
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
            elif act == FileAction.REMOVE and path.exists():
                await asyncio.to_thread(path.unlink)

    async def _handle_mqtt_file_mcu_read(self, ctx: Message, target: str) -> None:
        response_topic = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.FILE,
            FileAction.READ,
            protocol.MQTT_SUFFIX_RESPONSE,
            target,
        )
        async with self._mcu_read_lock:
            self._pending_mcu_read = _PendingMcuRead(target, asyncio.get_running_loop().create_future())
            if not await self.serial.send(
                Command.CMD_FILE_READ.value,
                msgspec.msgpack.encode(FileReadPacket(path=target[4:])),
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
            except asyncio.TimeoutError:
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
                cmd = (
                    msgspec.msgpack.decode(pl, type=ShellCommandPayload).command
                    if pl.startswith(b"\x81")
                    else pl.decode().strip()
                )
                pid = await self._run_process(cmd)
            except (msgspec.DecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
                logger.warning("MQTT shell run_async rejected", error=str(exc))
                payload = f"error:{exc}".encode()
            else:
                payload = str(pid).encode() if pid else b"error:internal"
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_path(
                        self.state.mqtt_topic_prefix,
                        Topic.SHELL,
                        ShellAction.RUN_ASYNC,
                        protocol.MQTT_SUFFIX_RESPONSE,
                    ),
                    payload,
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
                        msgspec.msgpack.encode(batch),
                        content_type="application/msgpack",
                    ),
                    reply_context=inbound,
                )
            else:
                await self._stop_process(pid)

    async def _handle_mqtt_spi(self, route: TopicRoute, inbound: Message) -> None:
        match route.identifier:
            case SpiAction.BEGIN:
                await self.serial.send(Command.CMD_SPI_BEGIN.value, b"")
            case SpiAction.END:
                await self.serial.send(Command.CMD_SPI_END.value, b"")
            case SpiAction.CONFIG:
                try:
                    p = msgspec.json.decode(inbound.payload, type=SpiConfigPacket)
                    await self.serial.send(Command.CMD_SPI_SET_CONFIG.value, msgspec.msgpack.encode(p))
                except (msgspec.DecodeError, msgspec.ValidationError, TypeError, ValueError) as exc:
                    logger.error("SPI config error: %s", exc)
            case SpiAction.TRANSFER:
                if inbound.payload:
                    res = await self.serial.send_and_wait_payload(
                        Command.CMD_SPI_TRANSFER.value,
                        msgspec.msgpack.encode(SpiTransferPacket(data=bytes(inbound.payload))),
                    )
                    if res:
                        await self.enqueue_mqtt(
                            QueuedPublish(
                                topic_path(
                                    self.state.mqtt_topic_prefix,
                                    Topic.SPI,
                                    SpiAction.TRANSFER,
                                    protocol.MQTT_SUFFIX_RESPONSE,
                                ),
                                msgspec.msgpack.decode(res, type=SpiTransferResponsePacket).data,
                            ),
                            reply_context=inbound,
                        )
            case _:
                return

    async def _handle_mqtt_pin(self, route: TopicRoute, inbound: Message) -> None:
        pin = self._parse_pin(route.segments[0])
        if pin < 0:
            return
        pl = msgspec.convert(inbound.payload, bytes).decode()
        if len(route.segments) == 2:
            if route.segments[1] == PinAction.MODE:
                await self.serial.send(
                    Command.CMD_SET_PIN_MODE.value, msgspec.msgpack.encode(PinModePacket(pin=pin, mode=int(pl)))
                )
            elif route.segments[1] == PinAction.READ:
                cmd = Command.CMD_DIGITAL_READ if route.topic == Topic.DIGITAL else Command.CMD_ANALOG_READ
                q = (
                    self.state.pending_digital_reads
                    if cmd == Command.CMD_DIGITAL_READ
                    else self.state.pending_analog_reads
                )
                if len(q) < self.state.pending_pin_request_limit:
                    q.append(structures.PendingPinRequest(pin=pin, reply_context=inbound))
                    await self.serial.send(cmd.value, msgspec.msgpack.encode(PinReadPacket(pin=pin)))
        else:
            cmd = Command.CMD_DIGITAL_WRITE if route.topic == Topic.DIGITAL else Command.CMD_ANALOG_WRITE
            await self.serial.send(
                cmd.value, msgspec.msgpack.encode(DigitalWritePacket(pin=pin, value=int(pl) if pl.isdigit() else 0))
            )

    async def _handle_mqtt_system(self, route: TopicRoute, inbound: Message) -> None:
        match route.identifier:
            case SystemAction.BOOTLOADER:
                await self.serial.send(
                    Command.CMD_ENTER_BOOTLOADER.value,
                    msgspec.msgpack.encode(EnterBootloaderPacket(magic=protocol.BOOTLOADER_MAGIC)),
                )
            case SystemAction.FREE_MEMORY if "get" in route.segments:
                pl = await self.serial.send_and_wait_payload(Command.CMD_GET_FREE_MEMORY.value, b"")
                if pl:
                    await self.enqueue_mqtt(
                        QueuedPublish(
                            topic_path(
                                self.state.mqtt_topic_prefix, Topic.SYSTEM, SystemAction.FREE_MEMORY, SystemAction.VALUE
                            ),
                            str(msgspec.msgpack.decode(pl, type=FreeMemoryResponsePacket).value).encode(),
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
                        msgspec.msgpack.encode(snap),
                        content_type="application/msgpack",
                    ),
                    reply_context=inbound,
                )
            case _:
                return

    # --- Low-level Helpers ---

    async def _request_mcu_version(self, inbound: Message | None = None) -> bool:
        pl = await self.serial.send_and_wait_payload(Command.CMD_GET_VERSION.value, b"")
        if pl:
            p = msgspec.msgpack.decode(pl, type=VersionResponsePacket)
            self.state.mcu_version = (p.major, p.minor, p.patch)
            await self._publish_version(self.state.mcu_version, inbound)
            return True
        return False

    async def _publish_version(self, v: tuple[int, int, int], ctx: Message | None) -> None:
        pl, tp = f"{v[0]}.{v[1]}.{v[2]}".encode(), topic_path(
            self.state.mqtt_topic_prefix, Topic.SYSTEM, SystemAction.VERSION, SystemAction.VALUE
        )
        await self.enqueue_mqtt(QueuedPublish(tp, pl, message_expiry_interval=MQTT_EXPIRY_DATASTORE))
        if ctx:
            await self.enqueue_mqtt(
                QueuedPublish(tp, pl, message_expiry_interval=MQTT_EXPIRY_DATASTORE), reply_context=ctx
            )

    async def _flush_console_queue(self) -> None:
        while self.state.console_to_mcu_queue and not self.state.mcu_is_paused:
            buf = self.state.console_to_mcu_queue.popleft()
            for chunk in itertools.batched(buf, protocol.MAX_PAYLOAD_SIZE):
                if not await self.serial.send(
                    Command.CMD_CONSOLE_WRITE.value, msgspec.msgpack.encode(ConsoleWritePacket(data=bytes(chunk)))
                ):
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
                except asyncio.TimeoutError:
                    import os
                    import signal

                    with contextlib.suppress(OSError):
                        os.killpg(ctx.handle.pid, signal.SIGKILL)
                    ctx.exit_code = -1
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
                    except asyncio.TimeoutError:
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
            import os
            import signal

            os.killpg(ctx.handle.pid, signal.SIGTERM)
            await asyncio.sleep(0.5)
            if ctx.handle.returncode is None:
                os.killpg(ctx.handle.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError) as exc:
            logger.warning("Process termination failed", pid=pid, error=str(exc))
        self._finalize_process(pid)
        return True

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
                usage = shutil.disk_usage(self.config.file_system_root)
                self.state.file_storage_bytes_used = usage.used
                if usage.free < len(data):
                    self.state.file_storage_limit_rejections += 1
                    return False
            except OSError:
                pass
            path.parent.mkdir(parents=True, exist_ok=True)
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
                msgspec.msgpack.encode({"status": "forbidden", "topic": val, "action": act}),
                content_type="application/msgpack",
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
