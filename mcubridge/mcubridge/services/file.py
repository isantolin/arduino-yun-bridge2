"""Filesystem component wrapping MCU and MQTT file operations."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from aiomqtt.message import Message

from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, FileAction, Status

from ..config.const import (
    FILE_LARGE_WARNING_BYTES,
    VOLATILE_STORAGE_PATHS,
)
from ..config.settings import RuntimeConfig
from ..protocol.structures import (
    FileReadPacket,
    FileReadResponsePacket,
    FileRemovePacket,
    FileWritePacket,
)
from ..protocol.topics import Topic, TopicRoute, topic_path
from ..state.context import RuntimeState
from ..util import chunk_bytes
from .base import BaseComponent, BridgeContext
import structlog

logger = structlog.get_logger("mcubridge.file")


@dataclass
class _PendingMcuRead:
    identifier: str
    future: asyncio.Future[bytes]
    chunks: list[bytes] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]


def _do_write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # [SIL-2] Atomic delegation to Path.write_bytes (C-backed)
    path.write_bytes(data)
    if path.stat().st_size > FILE_LARGE_WARNING_BYTES:
        logger.warning("File %s is growing large (>1MB) in RAM!", path)


class FileComponent(BaseComponent):
    """Encapsulate file read/write/remove logic."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        super().__init__(config, state, ctx)
        self._storage_lock = asyncio.Lock()
        self._usage_seeded = False
        self._metadata_cache: dict[str, dict[str, Any]] = {}
        self._mcu_read_lock = asyncio.Lock()
        self._pending_mcu_read: _PendingMcuRead | None = None
        self._mcu_backend_enabled = True

    async def handle_write(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_FILE_WRITE from MCU."""
        try:
            packet = FileWritePacket.decode(payload)
            path = self._get_safe_path(packet.path)
            if not path:
                await self.ctx.serial_flow.send(Status.ERROR.value, b"Invalid path")
                return False

            if not await self._write_with_quota(path, packet.data):
                await self.ctx.serial_flow.send(Status.ERROR.value, b"Quota exceeded")
                return False

            self._metadata_cache.pop(str(path), None)
            await self.ctx.serial_flow.send(Status.OK.value)
            return True
        except (ValueError, OSError) as e:
            logger.error("File write failed: %s", e)
            err_payload = str(e).encode("utf-8", errors="ignore")[
                : protocol.MAX_PAYLOAD_SIZE
            ]
            await self.ctx.serial_flow.send(Status.ERROR.value, err_payload)
            return False

    async def handle_read(self, seq_id: int, payload: bytes) -> None:
        """Handle CMD_FILE_READ from MCU."""
        try:
            packet = FileReadPacket.decode(payload)
            path = self._get_safe_path(packet.path)
            if not path or not path.is_file():
                await self.ctx.serial_flow.send(Status.ERROR.value, b"File not found")
                return
            data = await asyncio.to_thread(path.read_bytes)
            self._metadata_cache[str(path)] = {
                "size": len(data),
                "mtime": path.stat().st_mtime,
            }

            # [SIL-2] Use FileReadResponsePacket for consistent framing
            if not data:
                response_packet = FileReadResponsePacket(content=b"")
                await self.ctx.serial_flow.send(
                    Command.CMD_FILE_READ_RESP.value, response_packet.encode()
                )
            else:
                for chunk in chunk_bytes(data, protocol.MAX_PAYLOAD_SIZE - 3):
                    response_packet = FileReadResponsePacket(content=chunk)
                    await self.ctx.serial_flow.send(
                        Command.CMD_FILE_READ_RESP.value, response_packet.encode()
                    )
        except (ValueError, OSError) as e:
            logger.error("File read failed: %s", e)
            err_payload = str(e).encode("utf-8", errors="ignore")[
                : protocol.MAX_PAYLOAD_SIZE
            ]
            await self.ctx.serial_flow.send(Status.ERROR.value, err_payload)

    async def handle_remove(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_FILE_REMOVE from MCU."""
        try:
            packet = FileRemovePacket.decode(payload)
            path = self._get_safe_path(packet.path)
            if path and await self._remove_with_tracking(path):
                self._metadata_cache.pop(str(path), None)
                await self.ctx.serial_flow.send(Status.OK.value)
                return True

            await self.ctx.serial_flow.send(Status.ERROR.value, b"File not found")
            return False
        except (ValueError, OSError) as e:
            logger.error("File remove failed: %s", e)
            err_payload = str(e).encode("utf-8", errors="ignore")[
                : protocol.MAX_PAYLOAD_SIZE
            ]
            await self.ctx.serial_flow.send(Status.ERROR.value, err_payload)
            return False

    async def handle_read_response(self, seq_id: int, payload: bytes) -> bool:
        """Handle CMD_FILE_READ_RESP from MCU for MQTT-originated mcu/ reads."""
        pending = self._pending_mcu_read
        if pending is None:
            logger.warning("Received MCU file read response without pending request")
            return False

        try:
            packet = FileReadResponsePacket.decode(
                payload,
                Command.CMD_FILE_READ_RESP,
            )
        except ValueError:
            if not pending.future.done():
                pending.future.set_exception(
                    ValueError("Malformed MCU file read response")
                )
            return False

        if packet.content:
            pending.chunks.append(packet.content)
            return True

        if not pending.future.done():
            pending.future.set_result(b"".join(pending.chunks))
        return True

    async def handle_mqtt(
        self,
        route: TopicRoute,
        inbound: Message,
    ) -> bool:
        """Process MQTT filesystem requests."""
        action = route.action
        target = "/".join(route.remainder)
        pl = bytes(inbound.payload)

        if not action or not target:
            return False

        match action:
            case FileAction.WRITE:
                return await self._handle_mqtt_write(inbound, target, pl)
            case FileAction.READ:
                return await self._handle_mqtt_read(inbound, target)
            case FileAction.REMOVE:
                return await self._handle_mqtt_remove(inbound, target)
            case _:
                return False

    async def _handle_mqtt_write(
        self, inbound: Message, identifier: str, payload: bytes
    ) -> bool:
        if self._is_mcu_identifier(identifier):
            return await self._handle_mcu_write(inbound, identifier, payload)

        path = self._get_safe_path(identifier)
        if not path:
            await self._publish_mqtt_error(
                inbound, FileAction.WRITE, identifier, "Invalid path"
            )
            return False

        # Quota check
        if not await self._write_with_quota(path, payload):
            logger.error("MQTT write failed for %s: quota exceeded", identifier)
            await self._publish_mqtt_error(
                inbound,
                FileAction.WRITE,
                identifier,
                "Quota exceeded or invalid path",
            )
            return False

        self._metadata_cache.pop(str(path), None)
        return True

    async def _handle_mqtt_read(self, inbound: Message, identifier: str) -> bool:
        if self._is_mcu_identifier(identifier):
            return await self._handle_mcu_read(inbound, identifier)

        path = self._get_safe_path(identifier)
        if not path or not path.is_file():
            await self._publish_mqtt_error(
                inbound, FileAction.READ, identifier, "File not found"
            )
            return True

        try:
            data = await asyncio.to_thread(path.read_bytes)
            self._metadata_cache[str(path)] = {
                "size": len(data),
                "mtime": path.stat().st_mtime,
            }
            await self.state.publish(
                topic=self._mqtt_response_topic(FileAction.READ, identifier),
                payload=data,
                reply_to=inbound,
            )
            return True
        except OSError:
            return False

    async def _handle_mqtt_remove(self, inbound: Message, identifier: str) -> bool:
        if self._is_mcu_identifier(identifier):
            return await self._handle_mcu_remove(inbound, identifier)

        path = self._get_safe_path(identifier)
        if path and await self._remove_with_tracking(path):
            self._metadata_cache.pop(str(path), None)
            return True
        else:
            logger.error("MQTT remove failed for %s", identifier)
            await self._publish_mqtt_error(
                inbound,
                FileAction.REMOVE,
                identifier,
                "File not found or protected",
            )
            return False

    async def _handle_mcu_write(
        self, inbound: Message, identifier: str, payload: bytes
    ) -> bool:
        relative_path = self._normalise_mcu_identifier(identifier)
        if relative_path is None:
            await self._publish_mqtt_error(
                inbound, FileAction.WRITE, identifier, "Invalid path"
            )
            return False
        if not self._mcu_backend_enabled:
            logger.error(
                "MQTT write failed for %s: MCU filesystem backend is disabled",
                identifier,
            )
            await self._publish_mqtt_error(
                inbound,
                FileAction.WRITE,
                identifier,
                "MCU filesystem unavailable on this target",
            )
            return False

        packet = FileWritePacket(path=relative_path, data=payload).encode()
        if not await self.ctx.serial_flow.send(Command.CMD_FILE_WRITE.value, packet):
            logger.error("MQTT write failed for %s: MCU rejected write", identifier)
            await self._publish_mqtt_error(
                inbound,
                FileAction.WRITE,
                identifier,
                "MCU filesystem write failed",
            )
            return False
        return True

    async def _handle_mcu_read(self, inbound: Message, identifier: str) -> bool:
        relative_path = self._normalise_mcu_identifier(identifier)
        if relative_path is None:
            await self._publish_mqtt_error(
                inbound, FileAction.READ, identifier, "Invalid path"
            )
            return False
        if not self._mcu_backend_enabled:
            logger.error(
                "MQTT read failed for %s: MCU filesystem backend is disabled",
                identifier,
            )
            await self._publish_mqtt_error(
                inbound,
                FileAction.READ,
                identifier,
                "MCU filesystem unavailable on this target",
            )
            return False

        async with self._mcu_read_lock:
            pending = _PendingMcuRead(
                identifier=identifier,
                future=asyncio.get_running_loop().create_future(),
            )
            self._pending_mcu_read = pending
            packet = FileReadPacket(path=relative_path).encode()

            try:
                if not await self.ctx.serial_flow.send(Command.CMD_FILE_READ.value, packet):
                    logger.error(
                        "MQTT read failed for %s: MCU rejected read", identifier
                    )
                    await self._publish_mqtt_error(
                        inbound,
                        FileAction.READ,
                        identifier,
                        "MCU filesystem read failed",
                    )
                    return False

                timeout_seconds = self._mcu_read_timeout_seconds()
                data = await asyncio.wait_for(pending.future, timeout=timeout_seconds)
            except asyncio.TimeoutError:
                logger.error("MQTT read failed for %s: MCU read timed out", identifier)
                await self._publish_mqtt_error(
                    inbound,
                    FileAction.READ,
                    identifier,
                    "MCU filesystem read timed out",
                )
                return False
            except ValueError as exc:
                logger.error("MQTT read failed for %s: %s", identifier, exc)
                await self._publish_mqtt_error(
                    inbound, FileAction.READ, identifier, str(exc)
                )
                return False
            finally:
                if self._pending_mcu_read is pending:
                    self._pending_mcu_read = None

        await self.state.publish(
            topic=self._mqtt_response_topic(FileAction.READ, identifier),
            payload=data,
            reply_to=inbound,
        )
        return True

    async def _handle_mcu_remove(self, inbound: Message, identifier: str) -> bool:
        relative_path = self._normalise_mcu_identifier(identifier)
        if relative_path is None:
            await self._publish_mqtt_error(
                inbound, FileAction.REMOVE, identifier, "Invalid path"
            )
            return False
        if not self._mcu_backend_enabled:
            logger.error(
                "MQTT remove failed for %s: MCU filesystem backend is disabled",
                identifier,
            )
            await self._publish_mqtt_error(
                inbound,
                FileAction.REMOVE,
                identifier,
                "MCU filesystem unavailable on this target",
            )
            return False

        packet = FileRemovePacket(path=relative_path).encode()
        if not await self.ctx.serial_flow.send(Command.CMD_FILE_REMOVE.value, packet):
            logger.error("MQTT remove failed for %s: MCU rejected remove", identifier)
            await self._publish_mqtt_error(
                inbound,
                FileAction.REMOVE,
                identifier,
                "MCU filesystem remove failed",
            )
            return False
        return True

    def _get_safe_path(self, filename: str) -> Path | None:
        """Resolve and validate path within storage root (SIL-2)."""
        normalised = self._normalise_filename(filename)
        if not normalised or normalised == PurePosixPath("."):
            return None

        base_dir = self._get_base_dir()
        if not base_dir:
            return None

        try:
            safe_path = (base_dir / normalised).resolve()
            if str(safe_path).startswith(str(base_dir)):
                return safe_path
        except (OSError, RuntimeError) as e:
            logger.debug("Failed to close remote file descriptor: %s", e)
        return None

    def _get_base_dir(self) -> Path | None:
        """Return the validated base directory for file operations."""
        root = Path(self.config.file_system_root)
        if not self.config.allow_non_tmp_paths:
            if not any(str(root).startswith(p) for p in VOLATILE_STORAGE_PATHS):
                logger.error(
                    "FLASH PROTECTION: file_system_root %s is not in RAM!", root
                )
                return None
        try:
            root.mkdir(parents=True, exist_ok=True)
            return root
        except OSError:
            return None

    @staticmethod
    def _normalise_filename(filename: str) -> PurePosixPath | None:
        """Sanitize filename to prevent traversal attacks while allowing subdirectories."""
        if not filename or not filename.strip():
            return None
        if ".." in filename or filename.startswith("/") or "\x00" in filename:
            return None
        if filename in (".", "./", "../"):
            return None
        # Remove any leading path separators and keep only safe characters
        clean = re.sub(r"[^a-zA-Z0-9._/-]", "_", filename).strip(".")
        if not clean:
            return None
        return PurePosixPath(clean)

    @staticmethod
    def _is_mcu_identifier(identifier: str) -> bool:
        return identifier == "mcu" or identifier.startswith("mcu/")

    def _normalise_mcu_identifier(self, identifier: str) -> str | None:
        if not self._is_mcu_identifier(identifier):
            return None

        relative_identifier = identifier[4:] if identifier.startswith("mcu/") else ""
        normalised = self._normalise_filename(relative_identifier)
        if normalised is None or normalised == PurePosixPath("."):
            return None
        return normalised.as_posix()

    def _mqtt_response_topic(self, action: FileAction, identifier: str) -> str:
        return topic_path(
            self.state.mqtt_topic_prefix,
            Topic.FILE,
            action.value,
            "response",
            identifier,
        )

    async def _publish_mqtt_error(
        self,
        inbound: Message,
        action: FileAction,
        identifier: str,
        reason: str,
    ) -> None:
        await self.state.publish(
            topic=self._mqtt_response_topic(action, identifier),
            payload=reason.encode("utf-8", errors="ignore")[
                : protocol.MAX_PAYLOAD_SIZE
            ],
            reply_to=inbound,
        )

    def _mcu_read_timeout_seconds(self) -> float:
        timeout_ms = self.state.serial_response_timeout_ms
        if timeout_ms <= 0:
            return 1.0
        return max(timeout_ms / 1000.0, 1.0)

    async def _write_with_quota(self, path: Path, data: bytes) -> bool:
        async with self._storage_lock:
            if len(data) > self.config.file_write_max_bytes:
                self.state.file_write_limit_rejections += 1
                return False

            current_usage = await self._get_storage_usage()
            existing_size = path.stat().st_size if path.exists() else 0

            # [SIL-2] If usage tracking is stale, force refresh
            if existing_size > current_usage:
                await self._refresh_storage_usage()
                current_usage = self.state.file_storage_bytes_used

            projected_usage = current_usage - existing_size + len(data)

            if projected_usage > self.config.file_storage_quota_bytes:
                self.state.file_storage_limit_rejections += 1
                return False

            await asyncio.to_thread(_do_write_file, path, data)
            self.state.file_storage_bytes_used = projected_usage
            return True

    async def _remove_with_tracking(self, path: Path) -> bool:
        async with self._storage_lock:
            if not path.is_file():
                return False

            size = path.stat().st_size
            await asyncio.to_thread(path.unlink)
            self.state.file_storage_bytes_used = max(
                0, self.state.file_storage_bytes_used - size
            )
            return True

    async def _get_storage_usage(self) -> int:
        await self._ensure_usage_seeded()
        return self.state.file_storage_bytes_used

    async def _ensure_usage_seeded(self) -> None:
        if not self._usage_seeded:
            await self._refresh_storage_usage()
            self._usage_seeded = True

    async def _refresh_storage_usage(self) -> None:
        # [SIL-2] Use native Python scanning to calculate directory size.
        # This avoids multi-threading issues with os.fork() in libraries like 'sh'.
        try:
            root_path = Path(self.config.file_system_root)

            def _get_size():
                size = 0
                if root_path.exists():
                    for fp in root_path.rglob("*"):
                        if fp.is_file() and not fp.is_symlink():
                            size += fp.stat().st_size
                return size

            usage = await asyncio.to_thread(_get_size)
            self.state.file_storage_bytes_used = usage
        except (Exception, ValueError, IndexError, OSError):
            self.state.file_storage_bytes_used = 0


__all__ = ["FileComponent"]
