"""Filesystem component wrapping MCU and MQTT file operations."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

import zict
from aiomqtt.message import Message
from construct import ConstructError

from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, FileAction, Status

from ..config.const import (
    FILE_LARGE_WARNING_BYTES,
    VOLATILE_STORAGE_PATHS,
)
from ..config.settings import RuntimeConfig
from ..protocol.encoding import encode_status_reason
from ..protocol.structures import (
    FileReadPacket,
    FileReadResponsePacket,
    FileRemovePacket,
    FileWritePacket,
)
from ..protocol.topics import TopicRoute
from ..state.context import RuntimeState
from ..util import chunk_bytes
from .base import BaseComponent, BridgeContext

# Expose scandir for unit tests mocking it
scandir = os.scandir

logger = logging.getLogger("mcubridge.file")


def _do_write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # [SIL-2] Use 'wb' (write) for atomic consistency.
    # While the protocol frames are small, the current E2E expectations and
    # MQTT file writes assume the payload represents the full file content.
    with path.open("wb") as f:
        f.write(data)
        if f.tell() > FILE_LARGE_WARNING_BYTES:
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
        # [SIL-2] Metadata caching delegation to zict
        self._metadata_cache: zict.LRU[str, dict[str, Any]] = zict.LRU(100, {})

    async def handle_write(self, payload: bytes) -> bool:
        """Handle CMD_FILE_WRITE from MCU."""
        success, _, _ = await self._perform_file_operation("write", payload)
        return success

    async def handle_read(self, payload: bytes) -> None:
        """Handle CMD_FILE_READ from MCU."""
        await self._perform_file_operation("read", payload)

    async def handle_remove(self, payload: bytes) -> bool:
        """Handle CMD_FILE_REMOVE from MCU."""
        success, _, _ = await self._perform_file_operation("remove", payload)
        return success

    async def handle_mqtt(
        self,
        route: TopicRoute,
        msg: Message,
    ) -> bool:
        """Process MQTT filesystem requests."""
        action = route.action
        target = "/".join(route.remainder)
        pl = bytes(msg.payload)

        if not action or not target:
            return False

        match action:
            case FileAction.WRITE:
                return await self._handle_mqtt_write(msg, target, pl)
            case FileAction.READ:
                return await self._handle_mqtt_read(msg, target)
            case FileAction.REMOVE:
                return await self._handle_mqtt_remove(msg, target)
            case _:
                return False

    async def _handle_mqtt_write(self, inbound: Message, identifier: str, payload: bytes) -> bool:
        path = self._get_safe_path(identifier)
        if not path:
            return False

        # Quota check
        if not await self._write_with_quota(path, payload):
            logger.error("MQTT write failed for %s: quota exceeded", identifier)
            await self.ctx.publish(
                topic=str(inbound.topic),
                payload=encode_status_reason("Quota exceeded or invalid path"),
                reply_to=inbound,
            )
            return False

        self._metadata_cache.pop(str(path), None)
        return True

    async def _handle_mqtt_read(self, inbound: Message, identifier: str) -> bool:
        path = self._get_safe_path(identifier)
        if not path or not path.is_file():
            await self.ctx.publish(
                topic=str(inbound.topic),
                payload=encode_status_reason("File not found"),
                reply_to=inbound,
            )
            return True

        try:
            data = await asyncio.to_thread(path.read_bytes)
            self._metadata_cache[str(path)] = {"size": len(data), "mtime": path.stat().st_mtime}
            await self.ctx.publish(topic=str(inbound.topic), payload=data, reply_to=inbound)
            return True
        except OSError:
            return False

    async def _handle_mqtt_remove(self, inbound: Message, identifier: str) -> bool:
        path = self._get_safe_path(identifier)
        if path and await self._remove_with_tracking(path):
            self._metadata_cache.pop(str(path), None)
            return True
        else:
            logger.error("MQTT remove failed for %s", identifier)
            await self.ctx.publish(
                topic=str(inbound.topic),
                payload=encode_status_reason("File not found or protected"),
                reply_to=inbound,
            )
            return False

    async def _perform_file_operation(self, operation: str, payload: bytes) -> tuple[bool, bytes | None, str | None]:
        """Internal worker for MCU-originated file operations (legacy name kept for tests)."""
        try:
            if operation == "write":
                packet = FileWritePacket.decode(payload)
                path = self._get_safe_path(packet.path)
                if not path:
                    await self.ctx.send_frame(Status.ERROR.value, encode_status_reason("Invalid path"))
                    return False, None, "invalid_path"

                if not await self._write_with_quota(path, packet.data):
                    await self.ctx.send_frame(Status.ERROR.value, encode_status_reason("Quota exceeded"))
                    return False, None, "quota_exceeded"

                self._metadata_cache.pop(str(path), None)
                await self.ctx.send_frame(Status.OK.value)
                return True, b"OK", None
            elif operation == "read":
                packet = FileReadPacket.decode(payload)
                path = self._get_safe_path(packet.path)
                if not path or not path.is_file():
                    await self.ctx.send_frame(Status.ERROR.value, encode_status_reason("File not found"))
                    return False, None, "file_not_found"
                data = await asyncio.to_thread(path.read_bytes)
                self._metadata_cache[str(path)] = {"size": len(data), "mtime": path.stat().st_mtime}

                # [SIL-2] Use FileReadResponsePacket for consistent framing
                if not data:
                    response_packet = FileReadResponsePacket(content=b"")
                    await self.ctx.send_frame(Command.CMD_FILE_READ_RESP.value, response_packet.encode())
                else:
                    for chunk in chunk_bytes(data, protocol.MAX_PAYLOAD_SIZE - 2):
                        response_packet = FileReadResponsePacket(content=chunk)
                        await self.ctx.send_frame(Command.CMD_FILE_READ_RESP.value, response_packet.encode())
                return True, data, None
            elif operation == "remove":
                packet = FileRemovePacket.decode(payload)
                path = self._get_safe_path(packet.path)
                if path and await self._remove_with_tracking(path):
                    self._metadata_cache.pop(str(path), None)
                    await self.ctx.send_frame(Status.OK.value)
                    return True, b"OK", None

                await self.ctx.send_frame(Status.ERROR.value, encode_status_reason("File not found"))
                return False, None, "file_not_found"
        except (ConstructError, ValueError, OSError) as e:
            logger.error("File operation %s failed: %s", operation, e)
            await self.ctx.send_frame(Status.ERROR.value, encode_status_reason(str(e)))
            return False, None, str(e)

        return False, None, "unknown_operation"

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
        except (OSError, RuntimeError):
            pass
        return None

    def _get_base_dir(self) -> Path | None:
        """Return the validated base directory for file operations."""
        root = Path(self.config.file_system_root)
        if not self.config.allow_non_tmp_paths:
            if not any(str(root).startswith(p) for p in VOLATILE_STORAGE_PATHS):
                logger.error("FLASH PROTECTION: file_system_root %s is not in RAM!", root)
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
            self.state.file_storage_bytes_used = max(0, self.state.file_storage_bytes_used - size)
            return True

    async def _get_storage_usage(self) -> int:
        await self._ensure_usage_seeded()
        return self.state.file_storage_bytes_used

    async def _ensure_usage_seeded(self) -> None:
        if not self._usage_seeded:
            await self._refresh_storage_usage()
            self._usage_seeded = True

    async def _refresh_storage_usage(self) -> None:
        usage = await self._calculate_disk_usage(Path(self.config.file_system_root))
        self.state.file_storage_bytes_used = usage

    async def _calculate_disk_usage(self, path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        try:
            for entry in await asyncio.to_thread(scandir, str(path)):
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += await self._calculate_disk_usage(Path(entry.path))
        except OSError:
            pass
        return total
