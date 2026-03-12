"""Filesystem component wrapping MCU and MQTT file operations."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any

import zict
from aiomqtt.message import Message
from construct import ConstructError

from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, FileAction, Status

from ..config.const import (
    FILE_LARGE_WARNING_BYTES,
)
from ..config.settings import RuntimeConfig
from ..protocol.encoding import encode_status_reason
from ..protocol.structures import (
    FileReadPacket,
    FileRemovePacket,
    FileWritePacket,
)
from ..protocol.topics import Topic
from ..state.context import RuntimeState
from ..util import chunk_bytes
from .base import BaseComponent, BridgeContext

# Expose scandir for unit tests mocking it
scandir = os.scandir

logger = logging.getLogger("mcubridge.file")


def _do_write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # [SIL-2] Use 'ab' (append) to support chunked file writes from MCU.
    # Since the protocol is stateless and frames are max 64 bytes, large files
    # arrive as a sequence of CMD_FILE_WRITE. 'wb' would overwrite previous chunks.
    with path.open("ab") as f:
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
        try:
            packet = FileWritePacket.decode(payload)
            path = self._resolve_path(packet.path)
            await asyncio.to_thread(_do_write_file, path, packet.data)
            # Invalidate cache on write
            self._metadata_cache.pop(str(path), None)
            return True
        except (ConstructError, ValueError) as e:
            logger.error("Failed to parse file write: %s", e)
            return False

    async def handle_read(self, payload: bytes) -> None:
        """Handle CMD_FILE_READ from MCU."""
        try:
            packet = FileReadPacket.decode(payload)
            path = self._resolve_path(packet.path)
            if not path.is_file():
                return

            data = await asyncio.to_thread(path.read_bytes)
            # Cache metadata on read
            self._metadata_cache[str(path)] = {"size": len(data), "mtime": path.stat().st_mtime}

            for chunk in chunk_bytes(data, protocol.MAX_PAYLOAD_SIZE):
                await self.ctx.send_frame(Command.CMD_FILE_READ_RESP.value, chunk)
        except (ConstructError, ValueError) as e:
            logger.error("Failed to parse file read: %s", e)

    async def handle_remove(self, payload: bytes) -> bool:
        """Handle CMD_FILE_REMOVE from MCU."""
        try:
            packet = FileRemovePacket.decode(payload)
            path = self._resolve_path(packet.path)
            if path.is_file():
                await asyncio.to_thread(path.unlink)
                self._metadata_cache.pop(str(path), None)
                return True
            return False
        except (ConstructError, ValueError) as e:
            logger.error("Failed to parse file remove: %s", e)
            return False

    async def handle_mqtt(
        self,
        route: Any,
        inbound: Message,
    ) -> bool:
        """Process MQTT filesystem requests."""
        if route.topic != Topic.FILE:
            return False

        match route.action:
            case FileAction.READ:
                return await self._handle_mqtt_read(route, inbound)
            case FileAction.WRITE:
                return await self._handle_mqtt_write(route, inbound)
            case FileAction.REMOVE:
                return await self._handle_mqtt_remove(route, inbound)
            case _:
                return False

    async def _handle_mqtt_write(self, route: Any, inbound: Message) -> bool:
        path_str = route.identifier
        if not path_str:
            return False

        path = self._resolve_path(path_str)
        payload = bytes(inbound.payload) if inbound.payload else b""

        # Quota check
        if not await self._write_with_quota(path, payload):
            await self.ctx.publish(
                str(inbound.topic),
                encode_status_reason(Status.ERROR, "Quota exceeded or write failed"),
                reply_to=inbound,
            )
            return True

        self._metadata_cache.pop(str(path), None)
        await self.ctx.publish(str(inbound.topic), b"OK", reply_to=inbound)
        return True

    async def _handle_mqtt_read(self, route: Any, inbound: Message) -> bool:
        path_str = route.identifier
        if not path_str:
            return False

        path = self._resolve_path(path_str)
        if not path.is_file():
            await self.ctx.publish(
                str(inbound.topic),
                encode_status_reason(Status.ERROR, "File not found"),
                reply_to=inbound,
            )
            return True

        data = await asyncio.to_thread(path.read_bytes)
        self._metadata_cache[str(path)] = {"size": len(data), "mtime": path.stat().st_mtime}
        await self.ctx.publish(str(inbound.topic), data, reply_to=inbound)
        return True

    async def _handle_mqtt_remove(self, route: Any, inbound: Message) -> bool:
        path_str = route.identifier
        if not path_str:
            return False

        path = self._resolve_path(path_str)
        if await self._remove_with_tracking(path):
            self._metadata_cache.pop(str(path), None)
            await self.ctx.publish(str(inbound.topic), b"OK", reply_to=inbound)
        else:
            await self.ctx.publish(
                str(inbound.topic),
                encode_status_reason(Status.ERROR, "File not found or protected"),
                reply_to=inbound,
            )
        return True

    def _resolve_path(self, relative_path: str) -> Path:
        """Securely resolve a relative path within the file system root."""
        # Sanitize and join
        rel = PurePosixPath(relative_path.lstrip("/"))
        resolved = Path(self.config.file_system_root) / rel

        # [SIL-2] Security: Ensure the path is within the root
        if not str(resolved).startswith(self.config.file_system_root):
            raise ValueError(f"Security violation: path {relative_path} is outside root")

        return resolved

    async def _write_with_quota(self, path: Path, data: bytes) -> bool:
        async with self._storage_lock:
            if len(data) > self.config.file_write_max_bytes:
                self.state.file_write_limit_rejections += 1
                return False

            current_usage = await self._get_storage_usage()
            # If file exists, we subtract its current size from usage estimate
            existing_size = 0
            if path.exists():
                existing_size = path.stat().st_size

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
        """Get or calculate total storage usage in bytes."""
        if not self._usage_seeded:
            usage = await self._calculate_disk_usage(Path(self.config.file_system_root))
            self.state.file_storage_bytes_used = usage
            self._usage_seeded = True

        return self.state.file_storage_bytes_used

    async def _calculate_disk_usage(self, path: Path) -> int:
        """Calculate recursive disk usage via thread pool to avoid blocking."""
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
