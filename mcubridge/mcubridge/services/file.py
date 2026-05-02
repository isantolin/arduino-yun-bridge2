"""Filesystem component wrapping MCU and MQTT file operations."""

from __future__ import annotations

import asyncio
import structlog
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

import msgspec
from aiomqtt.message import Message

from mcubridge.protocol import protocol
from ..protocol.protocol import Command, FileAction, Status

from ..protocol.structures import (
    FileReadPacket,
    FileReadResponsePacket,
    FileRemovePacket,
    FileWritePacket,
    QueuedPublish,
)
from ..protocol.topics import Topic, TopicRoute, topic_path

if TYPE_CHECKING:
    from ..state.context import RuntimeState
    from ..config.settings import RuntimeConfig
    from .serial_flow import SerialFlowController

logger = structlog.get_logger("mcubridge.file")


@dataclass
class _PendingMcuRead:
    identifier: str
    future: asyncio.Future[bytes]
    chunks: list[bytes] = field(default_factory=lambda: cast(list[bytes], []))


class FileComponent:
    """Encapsulate file read/write/remove logic. [SIL-2]"""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_flow: SerialFlowController,
        enqueue_mqtt: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.serial_flow = serial_flow
        self.enqueue_mqtt = enqueue_mqtt
        self._storage_lock = asyncio.Lock()
        self._usage_seeded = False
        self._metadata_cache: dict[str, dict[str, Any]] = {}
        self._mcu_read_lock = asyncio.Lock()
        self._pending_mcu_read: _PendingMcuRead | None = None
        self._mcu_backend_enabled = True

    def _get_storage_usage(self) -> int:
        """Calculate current storage usage in bytes."""
        import shutil

        # [SIL-2] Use shutil.disk_usage for reliable space tracking
        try:
            usage = shutil.disk_usage(self.config.file_system_root)
            return usage.used
        except (OSError, ValueError):
            return 0

    async def handle_write(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_FILE_WRITE from MCU."""
        try:
            # [SIL-2] Use direct msgspec.msgpack.decode (Zero Wrapper)
            packet = msgspec.msgpack.decode(payload, type=FileWritePacket)
            path = self._get_safe_path(packet.path)
            if not path:
                await self.serial_flow.send(Status.ERROR.value, b"Invalid path")
                return False

            if not await self._write_with_quota(path, packet.data):
                await self.serial_flow.send(Status.ERROR.value, b"")
                return False

            self._metadata_cache.pop(str(path), None)
            await self.serial_flow.send(Status.OK.value, b"")
            return True
        except (ValueError, OSError, msgspec.DecodeError) as e:
            logger.error("File write failed: %s", e)
            err_payload = str(e).encode("utf-8", errors="ignore")[
                : protocol.MAX_PAYLOAD_SIZE
            ]
            await self.serial_flow.send(Status.ERROR.value, err_payload)
            return False

    async def handle_read(self, seq_id: int, payload: bytes) -> None:
        """Handle CMD_FILE_READ from MCU."""
        try:
            # [SIL-2] Use direct msgspec.msgpack.decode (Zero Wrapper)
            packet = msgspec.msgpack.decode(payload, type=FileReadPacket)
            path = self._get_safe_path(packet.path)
            if not path or not path.is_file():
                await self.serial_flow.send(Status.ERROR.value, b"")
                return

            data = await asyncio.to_thread(path.read_bytes)
            self._metadata_cache[str(path)] = {
                "size": len(data),
                "mtime": path.stat().st_mtime,
            }

            # [SIL-2] Use O(1) byte-splitting for RLE-style transmission
            chunk_gen = (
                data[i : i + protocol.MAX_PAYLOAD_SIZE]
                for i in range(0, len(data), protocol.MAX_PAYLOAD_SIZE)
            )

            for chunk in chunk_gen:
                resp = FileReadResponsePacket(content=chunk)
                await self.serial_flow.send(
                    Command.CMD_FILE_READ_RESP.value, msgspec.msgpack.encode(resp)
                )

            # EOT (End of Transmission) signalled by empty payload
            eot = FileReadResponsePacket(content=b"")
            await self.serial_flow.send(
                Command.CMD_FILE_READ_RESP.value, msgspec.msgpack.encode(eot)
            )

        except (OSError, msgspec.DecodeError) as e:
            logger.error("File read failed: %s", e)
            await self.serial_flow.send(Status.ERROR.value, str(e).encode())

    async def handle_remove(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_FILE_REMOVE from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=FileRemovePacket)
            path = self._get_safe_path(packet.path)
            if not path or not path.exists():
                await self.serial_flow.send(Status.ERROR.value, b"")
                return True

            await asyncio.to_thread(path.unlink)
            self._metadata_cache.pop(str(path), None)
            await self.serial_flow.send(Status.OK.value, b"")
            return True
        except (OSError, msgspec.DecodeError) as e:
            logger.error("File remove failed: %s", e)
            await self.serial_flow.send(Status.ERROR.value, str(e).encode())
            return False

    async def handle_read_response(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_FILE_READ_RESP from MCU."""
        try:
            packet = msgspec.msgpack.decode(payload, type=FileReadResponsePacket)
        except (ValueError, msgspec.DecodeError):
            return False

        async with self._mcu_read_lock:
            pending = self._pending_mcu_read
            if not pending:
                return False

            if packet.content:
                pending.chunks.append(packet.content)
                return True

            # EOF received
            if not pending.future.done():
                pending.future.set_result(b"".join(pending.chunks))
            return True

    async def handle_mqtt(self, route: TopicRoute, inbound: Message) -> bool:
        """Process inbound MQTT requests for file operations."""
        action = route.identifier
        identifier = "/".join(route.remainder)

        if not identifier:
            await self._mqtt_respond_error(inbound, action, identifier, "missing_path")
            return True

        match action:
            case FileAction.READ:
                if identifier.startswith("mcu/"):
                    return await self._handle_mcu_read(inbound, identifier[4:])
                return await self._handle_linux_read(inbound, identifier)
            case FileAction.WRITE:
                if identifier.startswith("mcu/"):
                    return await self._handle_mcu_write(inbound, identifier[4:])
                return await self._handle_linux_write(inbound, identifier)
            case FileAction.REMOVE:
                if identifier.startswith("mcu/"):
                    return await self._handle_mcu_remove(inbound, identifier[4:])
                return await self._handle_linux_remove(inbound, identifier)
            case _:
                return False

        return False

    async def _handle_linux_read(self, inbound: Message, identifier: str) -> bool:
        path = self._get_safe_path(identifier)
        if not path or not path.is_file():
            await self._mqtt_respond_error(
                inbound, FileAction.READ, identifier, "not_found"
            )
            return True

        try:
            data = await asyncio.to_thread(path.read_bytes)
            self._metadata_cache[str(path)] = {
                "size": len(data),
                "mtime": path.stat().st_mtime,
            }
            await self.enqueue_mqtt(
                QueuedPublish(
                    topic_name=self._mqtt_response_topic(FileAction.READ, identifier),
                    payload=data,
                ),
                reply_context=inbound,
            )
            return True
        except OSError as e:
            await self._mqtt_respond_error(inbound, FileAction.READ, identifier, str(e))
            return True

    async def _handle_linux_write(self, inbound: Message, identifier: str) -> bool:
        path = self._get_safe_path(identifier)
        if not path:
            await self._mqtt_respond_error(
                inbound, FileAction.WRITE, identifier, "invalid_path"
            )
            return True

        payload = msgspec.convert(inbound.payload, bytes)
        try:
            if not await self._write_with_quota(path, payload):
                await self._mqtt_respond_error(
                    inbound, FileAction.WRITE, identifier, "quota_exceeded"
                )
                return True

            self._metadata_cache.pop(str(path), None)
            await self._mqtt_respond_ok(inbound, FileAction.WRITE, identifier)
            return True
        except OSError as e:
            await self._mqtt_respond_error(
                inbound, FileAction.WRITE, identifier, str(e)
            )
            return True

    async def _handle_linux_remove(self, inbound: Message, identifier: str) -> bool:
        path = self._get_safe_path(identifier)
        if not path or not path.exists():
            await self._mqtt_respond_error(
                inbound, FileAction.REMOVE, identifier, "not_found"
            )
            return True

        try:
            await asyncio.to_thread(path.unlink)
            self._metadata_cache.pop(str(path), None)
            await self._mqtt_respond_ok(inbound, FileAction.REMOVE, identifier)
            return True
        except OSError as e:
            await self._mqtt_respond_error(
                inbound, FileAction.REMOVE, identifier, str(e)
            )
            return True

    async def _handle_mcu_read(self, inbound: Message, identifier: str) -> bool:
        if not self._mcu_backend_enabled:
            await self._mqtt_respond_error(
                inbound, FileAction.READ, identifier, "mcu_disabled"
            )
            return True

        async with self._mcu_read_lock:
            if self._pending_mcu_read:
                await self._mqtt_respond_error(
                    inbound, FileAction.READ, identifier, "mcu_busy"
                )
                return True

            future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            pending = _PendingMcuRead(identifier=identifier, future=future)
            self._pending_mcu_read = pending

            packet = FileReadPacket(path=identifier)
            if not await self.serial_flow.send(
                Command.CMD_FILE_READ.value, msgspec.msgpack.encode(packet)
            ):
                self._pending_mcu_read = None
                await self._mqtt_respond_error(
                    inbound, FileAction.READ, identifier, "mcu_dispatch_failed"
                )
                return True

            try:
                # [SIL-2] Deterministic timeout for MCU IO
                async with asyncio.timeout(30.0):
                    data = await future
            except asyncio.TimeoutError:
                await self._mqtt_respond_error(
                    inbound, FileAction.READ, identifier, "mcu_timeout"
                )
                return False
            finally:
                if self._pending_mcu_read is pending:
                    self._pending_mcu_read = None

        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=self._mqtt_response_topic(FileAction.READ, identifier),
                payload=data,
            ),
            reply_context=inbound,
        )
        return True

    async def _handle_mcu_write(self, inbound: Message, identifier: str) -> bool:
        if not self._mcu_backend_enabled:
            await self._mqtt_respond_error(
                inbound, FileAction.WRITE, identifier, "mcu_disabled"
            )
            return True

        payload = msgspec.convert(inbound.payload, bytes)
        packet = FileWritePacket(path=identifier, data=payload)
        ok = await self.serial_flow.send(
            Command.CMD_FILE_WRITE.value, msgspec.msgpack.encode(packet)
        )
        if ok:
            await self._mqtt_respond_ok(inbound, FileAction.WRITE, identifier)
        else:
            await self._mqtt_respond_error(
                inbound, FileAction.WRITE, identifier, "mcu_dispatch_failed"
            )
        return True

    async def _handle_mcu_remove(self, inbound: Message, identifier: str) -> bool:
        if not self._mcu_backend_enabled:
            await self._mqtt_respond_error(
                inbound, FileAction.REMOVE, identifier, "mcu_disabled"
            )
            return True

        packet = FileRemovePacket(path=identifier)
        ok = await self.serial_flow.send(
            Command.CMD_FILE_REMOVE.value, msgspec.msgpack.encode(packet)
        )
        if ok:
            await self._mqtt_respond_ok(inbound, FileAction.REMOVE, identifier)
        else:
            await self._mqtt_respond_error(
                inbound, FileAction.REMOVE, identifier, "mcu_dispatch_failed"
            )
        return True

    def _get_safe_path(self, identifier: str) -> Path | None:
        """[SIL-2] Strict path sanitation preventing directory traversal."""
        root = Path(self.config.file_system_root).resolve()
        # Handle PurePosixPath to ensure consistent path separators regardless of host OS
        requested = PurePosixPath(identifier)

        # Prevent absolute paths or backtracking
        if requested.is_absolute() or ".." in requested.parts:
            return None

        full_path = (root / requested).resolve()

        # Verify result is still within the root sandbox
        if not str(full_path).startswith(str(root)):
            return None

        return full_path

    async def _write_with_quota(self, path: Path, data: bytes) -> bool:
        """Write file data to disk with basic quota monitoring (best effort)."""
        async with self._storage_lock:
            # [SIL-2] Best-effort space tracking (shutil.disk_usage is O(1) on most OS)
            current_usage = self._get_storage_usage()
            if current_usage + len(data) > self.config.file_storage_quota_bytes:
                logger.warning("Storage quota exceeded; rejecting write.")
                return False

            path.parent.mkdir(parents=True, exist_ok=True)

            from ..config.const import FILE_LARGE_WARNING_BYTES

            if len(data) > FILE_LARGE_WARNING_BYTES:
                logger.warning("Writing large file: %s (%d bytes)", path, len(data))

            await asyncio.to_thread(path.write_bytes, data)
            return True

    def _mqtt_response_topic(self, action: FileAction | str, identifier: str) -> str:
        act_str = action.value if isinstance(action, FileAction) else str(action)
        return topic_path(
            self.state.mqtt_topic_prefix, Topic.FILE, act_str, "resp", identifier
        )

    async def _mqtt_respond_error(
        self, inbound: Message, action: FileAction | str, identifier: str, reason: str
    ) -> None:
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=self._mqtt_response_topic(action, identifier),
                payload=reason.encode("utf-8", errors="ignore")[
                    : protocol.MAX_PAYLOAD_SIZE
                ],
                user_properties=(("bridge-error", reason),),
            ),
            reply_context=inbound,
        )

    async def _mqtt_respond_ok(
        self,
        inbound: Message,
        action: FileAction | str,
        identifier: str,
        payload: bytes = b"OK",
    ) -> None:
        await self.enqueue_mqtt(
            QueuedPublish(
                topic_name=self._mqtt_response_topic(action, identifier),
                payload=payload,
            ),
            reply_context=inbound,
        )


__all__ = ["FileComponent"]
