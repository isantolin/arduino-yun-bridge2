"""Flattened Service Core for MCU and MQTT orchestration. [SIL-2]"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
import os
import shlex
from collections.abc import Coroutine, Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, Final

import msgspec
import psutil
import structlog
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
from ..state.context import RuntimeState

if TYPE_CHECKING:
    from ..transport.mqtt import MqttTransport
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
    chunks: list[bytes] = field(default_factory=cast(Callable[[], list[bytes]], list))


class BridgeService:
    """Consolidated Service Façade Eradicating Component Wrappers. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial: SerialTransport,
        mqtt: MqttTransport,
    ) -> None:
        self.config = config
        self.state = state
        self.serial = serial
        self.mqtt = mqtt
        self._task_group: asyncio.TaskGroup | None = None
        self._serial_sender: Callable[[int, bytes], Awaitable[bool]] | None = None

        # [SIL-2] Handshake Coordination
        from .handshake import SerialHandshakeManager, derive_serial_timing

        self.handshake = SerialHandshakeManager(
            config=config,
            state=state,
            serial_timing=derive_serial_timing(config),
            send_frame=self.serial.send,
            enqueue_mqtt=self.mqtt.enqueue_mqtt,
            acknowledge_frame=self.serial.acknowledge,
            logger_=logger,
        )

        # [SIL-2] Shared Resource Protection
        self._storage_lock = asyncio.Lock()
        self._mcu_read_lock = asyncio.Lock()
        self._pending_mcu_read: _PendingMcuRead | None = None
        self._process_slots = asyncio.Semaphore(int(state.process_max_concurrent))

        # [SIL-2] O(1) MCU Registry (Eradicates Component Delegation)
        self.mcu_registry: dict[int, McuHandler] = {
            Command.CMD_XOFF.value: self._handle_mcu_xoff,
            Command.CMD_XON.value: self._handle_mcu_xon,
            Command.CMD_CONSOLE_WRITE.value: self._handle_mcu_console_write,
            Command.CMD_DATASTORE_PUT.value: self._handle_mcu_datastore_put,
            Command.CMD_DATASTORE_GET.value: self._handle_mcu_datastore_get,
            Command.CMD_MAILBOX_PUSH.value: self._handle_mcu_mailbox_push,
            Command.CMD_MAILBOX_AVAILABLE.value: self._handle_mcu_mailbox_available,
            Command.CMD_MAILBOX_READ.value: self._handle_mcu_mailbox_read,
            Command.CMD_MAILBOX_PROCESSED.value: self._handle_mcu_mailbox_processed,
            Command.CMD_FILE_WRITE.value: self._handle_mcu_file_write,
            Command.CMD_FILE_READ.value: self._handle_mcu_file_read,
            Command.CMD_FILE_REMOVE.value: self._handle_mcu_file_remove,
            Command.CMD_FILE_READ_RESP.value: self._handle_mcu_file_read_resp,
            Command.CMD_PROCESS_RUN_ASYNC.value: self._handle_mcu_process_run,
            Command.CMD_PROCESS_POLL.value: self._handle_mcu_process_poll,
            Command.CMD_PROCESS_KILL.value: self._handle_mcu_process_kill,
            Command.CMD_DIGITAL_READ.value: self._handle_mcu_pin_digital_read,
            Command.CMD_ANALOG_READ.value: self._handle_mcu_pin_analog_read,
            Command.CMD_DIGITAL_READ_RESP.value: self._handle_mcu_pin_digital_read_resp,
            Command.CMD_ANALOG_READ_RESP.value: self._handle_mcu_pin_analog_read_resp,
            Command.CMD_SPI_TRANSFER_RESP.value: self._handle_mcu_spi_resp,
            Command.CMD_LINK_SYNC_RESP.value: self._handle_mcu_link_sync_resp,
            Command.CMD_LINK_RESET_RESP.value: self._handle_mcu_link_reset_resp,
            Status.ACK.value: self._handle_mcu_ack,
        }

        # Dynamic status handlers
        def make_handler(s_code: Status) -> McuHandler:
            return lambda seq, p: self._handle_mcu_status(seq, s_code, p)

        for s in Status:
            if s != Status.ACK:
                self.mcu_registry[s.value] = make_handler(s)

    def register_serial_sender(
        self, sender: Callable[[int, bytes], Awaitable[bool]]
    ) -> None:
        """Register the serial transport's send function."""
        self._serial_sender = sender

    # --- Lifecycle ---

    async def __aenter__(self) -> BridgeService:
        self._task_group = asyncio.TaskGroup()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(
        self,
        et: type[BaseException] | None,
        ev: BaseException | None,
        tb: Any,
    ) -> None:
        if self._task_group:
            await self._task_group.__aexit__(et, ev, tb)

    async def on_serial_connected(self) -> None:
        self.state.mark_transport_connected()
        try:
            await self.handshake.synchronize()
            if self.state.is_synchronized:
                await self._request_mcu_version()
                await self._flush_console_queue()
        except Exception as e:
            logger.exception("Sync failed: %s", e)

    async def on_serial_disconnected(self) -> None:
        self.state.mark_transport_disconnected()
        self.state.pending_digital_reads.clear()
        self.state.pending_analog_reads.clear()
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        self.handshake.clear_handshake_expectations()
        await self.serial.reset()

    # --- MCU Frame Dispatch ---

    async def handle_mcu_frame(self, cmd_id: int, seq_id: int, payload: bytes) -> None:
        if not (
            self.state.is_synchronized
            or cmd_id in _STATUS_VALUES
            or cmd_id in _PRE_SYNC_ALLOWED_COMMANDS
        ):
            logger.warning("Security: Rejecting pre-sync MCU frame 0x%02X", cmd_id)
            return

        if handler := self.mcu_registry.get(cmd_id):
            res = await handler(seq_id, payload)
            if res is not False and cmd_id not in _STATUS_VALUES:
                await self.serial.acknowledge(cmd_id, seq_id)
        elif response_to_request(cmd_id) is None:
            logger.warning("Protocol: Unhandled MCU command 0x%02X", cmd_id)
            self.state.metrics.unknown_command_count.inc()
            await self.serial.send(Status.NOT_IMPLEMENTED.value, b"")

    # --- MCU Handlers ---

    async def _handle_mcu_link_sync_resp(self, seq_id: int, payload: bytes) -> bool:
        return await self.handshake.handle_link_sync_resp(seq_id, payload)

    async def _handle_mcu_link_reset_resp(self, seq_id: int, payload: bytes) -> bool:
        return await self.handshake.handle_link_reset_resp(seq_id, payload)

    async def _handle_mcu_status(
        self, seq_id: int, status: Status, payload: bytes
    ) -> None:
        desc = status.description
        text = payload.decode("utf-8", errors="ignore") if payload else ""
        if status not in {Status.OK, Status.ACK}:
            logger.warning("MCU > %s: %s %s", status.name, desc, text)
        else:
            logger.debug("MCU > %s: %s %s", status.name, desc, text)

        await self.mqtt.enqueue_mqtt(
            QueuedPublish(
                topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS),
                msgspec.msgpack.encode(
                    {
                        "status": status.value,
                        "name": status.name,
                        "description": desc,
                        "message": text,
                    }
                ),
                content_type="application/msgpack",
                user_properties=(("bridge-status", status.name),),
            )
        )

    async def _handle_mcu_ack(self, seq_id: int, payload: bytes) -> None:
        if len(payload) >= 2:
            with contextlib.suppress(Exception):
                ack_target = msgspec.msgpack.decode(payload, type=AckPacket).command_id
                logger.debug("MCU > ACK for 0x%02X", ack_target)

    async def _handle_mcu_xoff(self, seq_id: int, _: bytes) -> None:
        self.state.mcu_is_paused = True
        self.state.serial_tx_allowed.clear()

    async def _handle_mcu_xon(self, seq_id: int, _: bytes) -> None:
        self.state.mcu_is_paused = False
        self.state.serial_tx_allowed.set()
        await self._flush_console_queue()

    async def _handle_mcu_console_write(self, seq_id: int, payload: bytes) -> None:
        with contextlib.suppress(Exception):
            p = msgspec.msgpack.decode(payload, type=ConsoleWritePacket)
            if p.data:
                await self.mqtt.enqueue_mqtt(
                    QueuedPublish(
                        topic_path(
                            self.state.mqtt_topic_prefix,
                            Topic.CONSOLE,
                            ConsoleAction.OUT,
                        ),
                        p.data,
                        message_expiry_interval=MQTT_EXPIRY_CONSOLE,
                    )
                )

    async def _handle_mcu_datastore_put(self, seq_id: int, payload: bytes) -> bool:
        with contextlib.suppress(Exception):
            p = msgspec.msgpack.decode(payload, type=DatastorePutPacket)
            if self.state.datastore_cache is not None:
                self.state.datastore_cache[p.key] = bytes(p.value)
            await self._publish_datastore_value(p.key, bytes(p.value))
            return True
        return False

    async def _handle_mcu_datastore_get(self, seq_id: int, payload: bytes) -> bool:
        with contextlib.suppress(Exception):
            key = msgspec.msgpack.decode(payload, type=DatastoreGetPacket).key
            # Use explicit cast to tell Pyright that get() returns bytes | str | None
            cache = cast(Any, self.state.datastore_cache)
            val = cache.get(key, b"") if cache else b""

            if isinstance(val, str):
                val = val.encode()

            res_payload = msgspec.convert(val, bytes)
            return await self.serial.send(
                Command.CMD_DATASTORE_GET_RESP.value,
                msgspec.msgpack.encode(
                    DatastoreGetResponsePacket(value=msgspec.Raw(res_payload[:255]))
                ),
            )
        return False

    async def _handle_mcu_mailbox_push(self, seq_id: int, payload: bytes) -> bool:
        with contextlib.suppress(Exception):
            p = msgspec.msgpack.decode(payload, type=MailboxPushPacket)
            await self.mqtt.enqueue_mqtt(
                QueuedPublish(
                    topic_path(
                        self.state.mqtt_topic_prefix,
                        Topic.MAILBOX,
                        MailboxAction.INCOMING,
                    ),
                    bytes(p.data),
                )
            )
            return True
        return False

    async def _handle_mcu_mailbox_available(self, seq_id: int, _: bytes) -> bool:
        return await self.serial.send(
            Command.CMD_MAILBOX_AVAILABLE_RESP.value,
            msgspec.msgpack.encode(
                MailboxAvailableResponsePacket(count=len(self.state.mailbox_queue))
            ),
        )

    async def _handle_mcu_mailbox_read(self, seq_id: int, _: bytes) -> bool:
        data = self.state.mailbox_queue.popleft() if self.state.mailbox_queue else b""
        return await self.serial.send(
            Command.CMD_MAILBOX_READ_RESP.value,
            msgspec.msgpack.encode(MailboxReadResponsePacket(content=data)),
        )

    async def _handle_mcu_mailbox_processed(self, seq_id: int, payload: bytes) -> bool:
        await self.mqtt.enqueue_mqtt(
            QueuedPublish(
                topic_path(
                    self.state.mqtt_topic_prefix, Topic.MAILBOX, MailboxAction.PROCESSED
                ),
                payload,
            )
        )
        return True

    async def _handle_mcu_file_write(self, seq_id: int, payload: bytes) -> bool:
        with contextlib.suppress(Exception):
            p = msgspec.msgpack.decode(payload, type=FileWritePacket)
            path = self._get_safe_path(p.path)
            if path and await self._write_with_quota(path, p.data):
                await self.serial.send(Status.OK.value, b"")
                return True
        await self.serial.send(Status.ERROR.value, b"Write failed")
        return False

    async def _handle_mcu_file_read(self, seq_id: int, payload: bytes) -> None:
        with contextlib.suppress(Exception):
            p = msgspec.msgpack.decode(payload, type=FileReadPacket)
            path = self._get_safe_path(p.path)
            if path and path.is_file():
                data = await asyncio.to_thread(path.read_bytes)
                if not data:
                    await self.serial.send(
                        Command.CMD_FILE_READ_RESP.value,
                        msgspec.msgpack.encode(FileReadResponsePacket(content=b"")),
                    )
                else:
                    for chunk in itertools.batched(data, protocol.MAX_PAYLOAD_SIZE - 3):
                        await self.serial.send(
                            Command.CMD_FILE_READ_RESP.value,
                            msgspec.msgpack.encode(
                                FileReadResponsePacket(content=bytes(chunk))
                            ),
                        )
                return
        await self.serial.send(Status.ERROR.value, b"Read failed")

    async def _handle_mcu_file_remove(self, seq_id: int, payload: bytes) -> bool:
        with contextlib.suppress(Exception):
            p = msgspec.msgpack.decode(payload, type=FileRemovePacket)
            path = self._get_safe_path(p.path)
            if path and path.exists():
                await asyncio.to_thread(path.unlink)
                await self.serial.send(Status.OK.value, b"")
                return True
        await self.serial.send(Status.ERROR.value, b"Remove failed")
        return False

    async def _handle_mcu_file_read_resp(self, seq_id: int, payload: bytes) -> bool:
        if not self._pending_mcu_read:
            return False
        with contextlib.suppress(Exception):
            p = msgspec.msgpack.decode(payload, type=FileReadResponsePacket)
            if p.content:
                self._pending_mcu_read.chunks.append(p.content)
                return True
            if not self._pending_mcu_read.future.done():
                self._pending_mcu_read.future.set_result(
                    b"".join(self._pending_mcu_read.chunks)
                )
            return True
        return False

    async def _handle_mcu_process_run(self, seq_id: int, payload: bytes) -> None:
        with contextlib.suppress(Exception):
            p = msgspec.msgpack.decode(payload, type=ProcessRunAsyncPacket)
            if p.command and self.state.allowed_policy.is_allowed(p.command):
                pid = await self._run_process(p.command)
                if pid:
                    await self.serial.send(
                        Command.CMD_PROCESS_RUN_ASYNC_RESP.value,
                        msgspec.msgpack.encode(ProcessRunAsyncResponsePacket(pid=pid)),
                    )
                    return
        await self.serial.send(Status.ERROR.value, b"Exec failed")

    async def _handle_mcu_process_poll(self, seq_id: int, payload: bytes) -> None:
        with contextlib.suppress(Exception):
            pid = msgspec.msgpack.decode(payload, type=ProcessPollPacket).pid
            batch = await self._poll_process(pid)
            await self.serial.send(
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

    async def _handle_mcu_process_kill(self, seq_id: int, payload: bytes) -> None:
        with contextlib.suppress(Exception):
            pid = msgspec.msgpack.decode(payload, type=ProcessKillPacket).pid
            await self._stop_process(pid)

    async def _handle_mcu_pin_digital_read(self, seq_id: int, payload: bytes) -> bool:
        return await self.serial.send(
            Status.NOT_IMPLEMENTED.value, b"linux_gpio_read_not_available"
        )

    async def _handle_mcu_pin_analog_read(self, seq_id: int, payload: bytes) -> bool:
        return await self.serial.send(
            Status.NOT_IMPLEMENTED.value, b"linux_adc_read_not_available"
        )

    async def _handle_mcu_pin_digital_read_resp(
        self, seq_id: int, payload: bytes
    ) -> None:
        await self._handle_pin_resp(
            payload,
            Topic.DIGITAL,
            DigitalReadResponsePacket,
            self.state.pending_digital_reads,
        )

    async def _handle_mcu_pin_analog_read_resp(
        self, seq_id: int, payload: bytes
    ) -> None:
        await self._handle_pin_resp(
            payload,
            Topic.ANALOG,
            AnalogReadResponsePacket,
            self.state.pending_analog_reads,
        )

    async def _handle_mcu_spi_resp(self, seq_id: int, payload: bytes) -> bool:
        with contextlib.suppress(Exception):
            p = msgspec.msgpack.decode(payload, type=SpiTransferResponsePacket)
            await self.mqtt.enqueue_mqtt(
                QueuedPublish(
                    topic_path(
                        self.state.mqtt_topic_prefix, Topic.SPI, "transfer", "resp"
                    ),
                    p.data,
                )
            )
            return True
        return False

    # --- MQTT Dispatch ---

    async def handle_mqtt_message(self, inbound: Message) -> None:
        if route := parse_topic(self.state.mqtt_topic_prefix, str(inbound.topic)):
            if route.topic != Topic.SYSTEM:
                with contextlib.suppress(asyncio.TimeoutError):
                    async with asyncio.timeout(30.0):
                        await self.state.link_sync_event.wait()

            # Policy Guard
            action = self._deduce_action(route)
            if action and not (
                self.state.topic_authorization.allows(
                    (
                        route.topic.value
                        if isinstance(route.topic, Topic)
                        else route.topic
                    ),
                    action,
                )
                if self.state.topic_authorization
                else False
            ):
                await self._reject_mqtt(inbound, route.topic, action)
                return

            # Direct Match Dispatch (Eradicates dynamic registries)
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

    # --- MQTT Handlers ---

    async def _handle_mqtt_console(self, inbound: Message) -> None:
        if pl := msgspec.convert(inbound.payload, bytes):
            self.state.console_to_mcu_queue.append(pl)
            await self._flush_console_queue()

    async def _handle_mqtt_datastore(self, route: TopicRoute, inbound: Message) -> None:
        # Strip '/request' or '/response' if present to get the clean key
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
            is_req = bool(route.remainder) and route.remainder[-1] == "request"
            cache = cast(Any, self.state.datastore_cache)
            val = cache.get(key) if cache else None

            if val is not None:
                if not is_req and inbound.payload:
                    return

                res_payload = msgspec.convert(
                    val.encode() if isinstance(val, str) else val, bytes
                )
                await self._publish_datastore_value(
                    key, res_payload, reply_context=inbound
                )
            elif is_req:
                await self._publish_datastore_value(
                    key, b"", reply_context=inbound, error="datastore-miss"
                )

    async def _handle_mqtt_mailbox(self, route: TopicRoute, inbound: Message) -> None:
        pl = msgspec.convert(inbound.payload, bytes)
        if route.identifier == MailboxAction.WRITE:
            self.state.mailbox_queue.append(pl)
            await self.serial.send(
                Command.CMD_MAILBOX_PUSH.value,
                msgspec.msgpack.encode(MailboxPushPacket(data=pl)),
            )
        elif route.identifier == MailboxAction.READ:
            await self.serial.send(Command.CMD_MAILBOX_READ.value, b"")

    async def _handle_mqtt_file(self, route: TopicRoute, inbound: Message) -> None:
        action = route.action
        target = "/".join(route.remainder)
        pl = msgspec.convert(inbound.payload, bytes)
        if not (action and target):
            return
        if target.startswith("mcu/"):
            if action == FileAction.READ:
                await self._handle_mqtt_file_mcu_read(inbound, target)
            elif action == FileAction.WRITE:
                await self.serial.send(
                    Command.CMD_FILE_WRITE.value,
                    msgspec.msgpack.encode(FileWritePacket(path=target[4:], data=pl)),
                )
            elif action == FileAction.REMOVE:
                await self.serial.send(
                    Command.CMD_FILE_REMOVE.value,
                    msgspec.msgpack.encode(FileRemovePacket(path=target[4:])),
                )
        else:
            path = self._get_safe_path(target)
            if not path:
                return
            if action == FileAction.WRITE:
                if await self._write_with_quota(path, pl):
                    await self.mqtt.enqueue_mqtt(
                        QueuedPublish(
                            topic_path(
                                self.state.mqtt_topic_prefix,
                                Topic.FILE,
                                FileAction.READ,
                                target,
                            ),
                            pl,
                        ),
                        reply_context=inbound,
                    )
            elif action == FileAction.READ and path.is_file():
                if inbound.topic.value.endswith(protocol.MQTT_SUFFIX_RESPONSE):
                    return
                await self.mqtt.enqueue_mqtt(
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
            elif action == FileAction.REMOVE and path.exists():
                await asyncio.to_thread(path.unlink)

    async def _handle_mqtt_shell(self, route: TopicRoute, inbound: Message) -> None:
        action = route.segments[0] if route.segments else None
        pl = msgspec.convert(inbound.payload, bytes)
        if action == ShellAction.RUN_ASYNC:
            with contextlib.suppress(Exception):
                cmd = (
                    msgspec.msgpack.decode(pl, type=ShellCommandPayload).command
                    if pl.startswith(b"\x81")
                    else pl.decode().strip()
                )
                pid = await self._run_process(cmd)
                await self.mqtt.enqueue_mqtt(
                    QueuedPublish(
                        topic_path(
                            self.state.mqtt_topic_prefix,
                            Topic.SHELL,
                            ShellAction.RUN_ASYNC,
                            protocol.MQTT_SUFFIX_RESPONSE,
                        ),
                        str(pid).encode() if pid else b"error:internal",
                    ),
                    reply_context=inbound,
                )
        elif (
            action in (ShellAction.POLL, ShellAction.KILL) and len(route.segments) == 2
        ):
            pid = int(route.segments[1])
            if action == ShellAction.POLL:
                batch = await self._poll_process(pid)
                await self.mqtt.enqueue_mqtt(
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
                with contextlib.suppress(Exception):
                    # Simplified raw decoding
                    raw = msgspec.json.decode(inbound.payload)
                    p = msgspec.convert(raw, SpiConfigPacket)
                    await self.serial.send(
                        Command.CMD_SPI_SET_CONFIG.value, msgspec.msgpack.encode(p)
                    )
            case SpiAction.TRANSFER:
                if inbound.payload:
                    payload = msgspec.msgpack.encode(
                        SpiTransferPacket(data=bytes(inbound.payload))
                    )
                    res = await self.serial.send_and_wait_payload(
                        Command.CMD_SPI_TRANSFER.value, payload
                    )
                    if res:
                        p = msgspec.msgpack.decode(res, type=SpiTransferResponsePacket)
                        await self.mqtt.enqueue_mqtt(
                            QueuedPublish(
                                topic_path(
                                    self.state.mqtt_topic_prefix,
                                    Topic.SPI,
                                    SpiAction.TRANSFER,
                                    protocol.MQTT_SUFFIX_RESPONSE,
                                ),
                                p.data,
                            ),
                            reply_context=inbound,
                        )
            case _:
                logger.warning("Unsupported SPI action: %s", route.identifier)

    async def _handle_mqtt_pin(self, route: TopicRoute, inbound: Message) -> None:
        pin = self._parse_pin(route.segments[0])
        pl = msgspec.convert(inbound.payload, bytes).decode()
        if pin < 0:
            return
        match len(route.segments):
            case 2:
                if route.segments[1] == PinAction.MODE:
                    await self.serial.send(
                        Command.CMD_SET_PIN_MODE.value,
                        msgspec.msgpack.encode(PinModePacket(pin=pin, mode=int(pl))),
                    )
                elif route.segments[1] == PinAction.READ:
                    cmd = (
                        Command.CMD_DIGITAL_READ
                        if route.topic == Topic.DIGITAL
                        else Command.CMD_ANALOG_READ
                    )
                    queue = (
                        self.state.pending_digital_reads
                        if cmd == Command.CMD_DIGITAL_READ
                        else self.state.pending_analog_reads
                    )
                    if len(queue) < self.state.pending_pin_request_limit:
                        queue.append(
                            structures.PendingPinRequest(pin=pin, reply_context=inbound)
                        )
                        await self.serial.send(
                            cmd.value, msgspec.msgpack.encode(PinReadPacket(pin=pin))
                        )
            case 1:
                cmd = (
                    Command.CMD_DIGITAL_WRITE
                    if route.topic == Topic.DIGITAL
                    else Command.CMD_ANALOG_WRITE
                )
                val = int(pl) if pl.isdigit() else 0
                await self.serial.send(
                    cmd.value,
                    msgspec.msgpack.encode(DigitalWritePacket(pin=pin, value=val)),
                )
            case _:
                pass

    async def _handle_mqtt_system(self, route: TopicRoute, inbound: Message) -> None:
        match route.identifier:
            case SystemAction.BOOTLOADER:
                await self.serial.send(
                    Command.CMD_ENTER_BOOTLOADER.value,
                    msgspec.msgpack.encode(
                        EnterBootloaderPacket(magic=protocol.BOOTLOADER_MAGIC)
                    ),
                )
            case SystemAction.FREE_MEMORY if "get" in route.segments:
                pl = await self.serial.send_and_wait_payload(
                    Command.CMD_GET_FREE_MEMORY.value, b""
                )
                if pl:
                    val = str(
                        msgspec.msgpack.decode(pl, type=FreeMemoryResponsePacket).value
                    ).encode()
                    await self.mqtt.enqueue_mqtt(
                        QueuedPublish(
                            topic_path(
                                self.state.mqtt_topic_prefix,
                                Topic.SYSTEM,
                                SystemAction.FREE_MEMORY,
                                SystemAction.VALUE,
                            ),
                            val,
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
                await self.mqtt.enqueue_mqtt(
                    QueuedPublish(
                        topic_path(
                            self.state.mqtt_topic_prefix,
                            Topic.SYSTEM,
                            "bridge",
                            flavor,
                            "value",
                        ),
                        msgspec.msgpack.encode(snap),
                        content_type="application/msgpack",
                    ),
                    reply_context=inbound,
                )
            case _:
                pass

    # --- Helpers ---

    async def _request_mcu_version(self, inbound: Message | None = None) -> bool:
        pl = await self.serial.send_and_wait_payload(Command.CMD_GET_VERSION.value, b"")
        if pl:
            p = msgspec.msgpack.decode(pl, type=VersionResponsePacket)
            v = (p.major, p.minor, p.patch)
            self.state.mcu_version = v
            await self._publish_version(v, inbound)
            return True
        return False

    async def _publish_version(
        self, v: tuple[int, int, int], ctx: Message | None
    ) -> None:
        pl = f"{v[0]}.{v[1]}.{v[2]}".encode()
        tp = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.SYSTEM,
            SystemAction.VERSION,
            SystemAction.VALUE,
        )
        await self.mqtt.enqueue_mqtt(
            QueuedPublish(tp, pl, message_expiry_interval=MQTT_EXPIRY_DATASTORE)
        )
        if ctx:
            await self.mqtt.enqueue_mqtt(
                QueuedPublish(tp, pl, message_expiry_interval=MQTT_EXPIRY_DATASTORE),
                reply_context=ctx,
            )

    async def _flush_console_queue(self) -> None:
        while self.state.console_to_mcu_queue and not self.state.mcu_is_paused:
            buf = self.state.console_to_mcu_queue.popleft()
            for chunk in itertools.batched(buf, protocol.MAX_PAYLOAD_SIZE):
                if not await self.serial.send(
                    Command.CMD_CONSOLE_WRITE.value,
                    msgspec.msgpack.encode(ConsoleWritePacket(data=bytes(chunk))),
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
                self.state.running_processes[pid] = p
                self.state.process_io_locks[pid] = asyncio.Lock()
                self.state.process_exit_codes[pid] = 0
            asyncio.create_task(self._monitor_process(pid))
            return pid
        except OSError:
            self._process_slots.release()
            return 0

    async def _monitor_process(self, pid: int) -> None:
        try:
            async with self.state.process_lock:
                h = self.state.running_processes.get(pid)
            if h:
                try:
                    self.state.process_exit_codes[pid] = await asyncio.wait_for(
                        h.wait(), float(self.state.process_timeout)
                    )
                except asyncio.TimeoutError:
                    with contextlib.suppress(OSError):
                        h.kill()
                    self.state.process_exit_codes[pid] = -1
        finally:
            self._finalize_process(pid)

    async def _poll_process(self, pid: int) -> ProcessOutputBatch:
        async with self.state.process_lock:
            h = self.state.running_processes.get(pid)
            io_lock = self.state.process_io_locks.get(pid)
            ec = self.state.process_exit_codes.get(pid, 0)
            if not h or not io_lock:
                return ProcessOutputBatch(
                    Status.ERROR.value, 1, b"", b"", True, False, False
                )

            async with io_lock:

                async def _rd(s: asyncio.StreamReader | None) -> tuple[bytes, bool]:
                    if not s or s.at_eof():
                        return b"", False
                    try:
                        c = await asyncio.wait_for(
                            s.read(protocol.MAX_PAYLOAD_SIZE - 32), 0.01
                        )
                        return c, not s.at_eof()
                    except asyncio.TimeoutError:
                        return b"", True

                o, to = await _rd(h.stdout)
                e, te = await _rd(h.stderr)
                fin = h.returncode is not None
                b = ProcessOutputBatch(Status.OK.value, ec, o, e, fin, to, te)
                if (
                    fin
                    and (h.stdout is None or h.stdout.at_eof())
                    and (h.stderr is None or h.stderr.at_eof())
                ):
                    self._finalize_process(pid)
                return b

    async def _stop_process(self, pid: int) -> bool:
        async with self.state.process_lock:
            h = self.state.running_processes.get(pid)
        if not h:
            return False
        try:
            p = psutil.Process(h.pid)
            all_p = p.children(recursive=True) + [p]
            for proc in all_p:
                with contextlib.suppress(Exception):
                    proc.terminate()
            psutil.wait_procs(all_p, timeout=3.0)
            for proc in all_p:
                with contextlib.suppress(Exception):
                    proc.kill()
        except Exception:
            pass
        self._finalize_process(pid)
        return True

    def _finalize_process(self, pid: int) -> None:
        if self.state.running_processes.pop(pid, None):
            self.state.process_io_locks.pop(pid, None)
            self._process_slots.release()

    async def _handle_pin_resp(
        self,
        pl: bytes,
        tp: Topic,
        cls: Any,
        q: collections.deque[structures.PendingPinRequest],
    ) -> None:
        with contextlib.suppress(Exception):
            v = msgspec.msgpack.decode(pl, type=cls).value
            req = q.popleft() if q else None
            await self.mqtt.enqueue_mqtt(
                QueuedPublish(
                    topic_path(
                        self.state.mqtt_topic_prefix,
                        tp,
                        str(req.pin) if req else "unknown",
                        "value",
                    ),
                    str(v).encode(),
                    message_expiry_interval=MQTT_EXPIRY_PIN,
                    user_properties=(
                        ("bridge-pin", str(req.pin) if req else "unknown"),
                    ),
                ),
                reply_context=req.reply_context if req else None,
            )

    async def _handle_mqtt_file_mcu_read(self, ctx: Message, target: str) -> None:
        async with self._mcu_read_lock:
            self._pending_mcu_read = _PendingMcuRead(
                target, asyncio.get_running_loop().create_future()
            )
            if await self.serial.send(
                Command.CMD_FILE_READ.value,
                msgspec.msgpack.encode(FileReadPacket(path=target[4:])),
            ):
                try:
                    res = await asyncio.wait_for(self._pending_mcu_read.future, 30.0)
                    await self.mqtt.enqueue_mqtt(
                        QueuedPublish(
                            topic_path(
                                self.state.mqtt_topic_prefix,
                                Topic.FILE,
                                FileAction.READ,
                                target,
                            ),
                            res,
                        ),
                        reply_context=ctx,
                    )
                except asyncio.TimeoutError:
                    pass
            self._pending_mcu_read = None

    def _get_safe_path(self, p_str: str) -> Path | None:
        p = Path(self.config.file_system_root).joinpath(p_str.lstrip("/")).resolve()
        return (
            p
            if str(p).startswith(os.path.abspath(self.config.file_system_root))
            else None
        )

    async def _write_with_quota(self, path: Path, data: bytes) -> bool:
        async with self._storage_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, data)
            return True

    def _parse_pin(self, s: str) -> int:
        s = s.upper()
        return (
            int(s[1:])
            if s.startswith("A") and s[1:].isdigit()
            else (int(s) if s.isdigit() else -1)
        )

    def _deduce_action(self, r: TopicRoute) -> str | None:
        if r.topic == Topic.SYSTEM:
            return None
        if r.topic in (Topic.DIGITAL, Topic.ANALOG):
            return (
                "write"
                if len(r.segments) == 1
                else (r.segments[1].lower() if len(r.segments) > 1 else None)
            )
        return (
            "in"
            if r.topic == Topic.CONSOLE and r.identifier == "in"
            else (r.identifier or None)
        )

    async def _reject_mqtt(self, ctx: Message, tp: Topic | str, act: str) -> None:
        val = tp.value if isinstance(tp, Topic) else tp
        await self.mqtt.enqueue_mqtt(
            QueuedPublish(
                topic_path(self.state.mqtt_topic_prefix, Topic.SYSTEM, Topic.STATUS),
                msgspec.msgpack.encode(
                    {"status": "forbidden", "topic": val, "action": act}
                ),
                content_type="application/msgpack",
                user_properties=(("bridge-error", TOPIC_FORBIDDEN_REASON),),
            ),
            reply_context=ctx,
        )

    async def _publish_datastore_value(
        self,
        key: str,
        val: bytes,
        reply_context: Message | None = None,
        error: str | None = None,
    ) -> None:
        tp = topic_path(
            self.state.mqtt_topic_prefix,
            Topic.DATASTORE,
            DatastoreAction.GET,
            *filter(None, key.split("/")),
        )
        props = (
            (("bridge-datastore-key", key), ("bridge-error", error))
            if error
            else (("bridge-datastore-key", key),)
        )
        pub = QueuedPublish(
            tp,
            val,
            message_expiry_interval=MQTT_EXPIRY_DATASTORE,
            user_properties=props,
        )
        await self.mqtt.enqueue_mqtt(pub)
        if reply_context:
            await self.mqtt.enqueue_mqtt(pub, reply_context=reply_context)
