"""Filesystem component wrapping MCU and MQTT file operations."""

from __future__ import annotations

import logging
import os
import shutil
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiofiles
from aiomqtt import Message as MQTTMessage

from yunbridge.protocol import Topic
from yunbridge.rpc.protocol import UINT16_FORMAT

logger = logging.getLogger("yunbridge.file")


class FileComponent:
    """Handles file system operations requested by MCU or MQTT."""

    def __init__(
        self,
        root_path: str,
        send_frame: Callable[[int, bytes], Awaitable[bool]],
        publish_mqtt: Callable[[str, bytes | str, bool], Awaitable[None]],
        write_max_bytes: int,
        storage_quota_bytes: int,
    ) -> None:
        self.root = Path(root_path).resolve()
        self.send_frame = send_frame
        self.publish_mqtt = publish_mqtt
        self.write_max_bytes = write_max_bytes
        self.storage_quota_bytes = storage_quota_bytes

        if not self.root.exists():
            logger.warning("Filesystem root %s does not exist; creating.", self.root)
            self.root.mkdir(parents=True, exist_ok=True)

    async def handle_write(self, payload: bytes) -> bool:
        """Handle CMD_FILE_WRITE (0x50) from MCU."""
        if len(payload) < 3:
            return False

        try:
            path_len = payload[0]
            if len(payload) < 1 + path_len + 2:
                return False

            path_bytes = payload[1 : 1 + path_len]
            cursor = 1 + path_len
            data_len = struct.unpack(UINT16_FORMAT, payload[cursor : cursor + 2])[0]
            cursor += 2

            if len(payload) < cursor + data_len:
                return False

            data = payload[cursor : cursor + data_len]
            relative_path = path_bytes.decode("utf-8", errors="ignore")

            target_path = self._get_safe_path(relative_path)
            if not target_path:
                logger.warning("Blocked unsafe file write path: %s", relative_path)
                return True  # Handled (rejected)

            if len(data) > self.write_max_bytes:
                logger.warning("File write exceeds per-operation limit (%d bytes)", len(data))
                return True

            if not self._check_quota(len(data)):
                logger.warning("File write rejected: Storage quota exceeded.")
                return True

            # Ensure parent directory exists
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # Write async
            async with aiofiles.open(target_path, "wb") as f:
                await f.write(data)

            logger.debug("Wrote %d bytes to %s", len(data), target_path)
            return True

        except Exception:
            logger.exception("Error handling CMD_FILE_WRITE")
            return False

    async def handle_read(self, payload: bytes) -> bool:
        """Handle CMD_FILE_READ (0x51) is not typically initiated by MCU in this protocol version."""
        # Reserved for future MCU-initiated reads if needed.
        return True

    async def handle_remove(self, payload: bytes) -> bool:
        """Handle CMD_FILE_REMOVE (0x52) from MCU."""
        try:
            if not payload:
                return False
            path_len = payload[0]
            if len(payload) < 1 + path_len:
                return False

            path_str = payload[1 : 1 + path_len].decode("utf-8", errors="ignore")
            target_path = self._get_safe_path(path_str)

            if target_path and target_path.exists() and target_path.is_file():
                os.remove(target_path)
                logger.debug("Removed file %s", target_path)

            return True
        except Exception:
            logger.exception("Error handling CMD_FILE_REMOVE")
            return False

    async def handle_mqtt(
        self,
        identifier: str,
        segments: list[str],
        payload: bytes,
        message: MQTTMessage,
    ) -> None:
        """Dispatch MQTT requests for file operations."""
        if identifier == "write":
            await self._handle_mqtt_write(segments, payload)
        elif identifier == "read":
            await self._handle_mqtt_read(segments)
        elif identifier == "remove":
            await self._handle_mqtt_remove(segments)

    async def _handle_mqtt_write(self, segments: list[str], payload: bytes) -> None:
        if not segments:
            return

        rel_path = "/".join(segments)
        target_path = self._get_safe_path(rel_path)
        if not target_path:
            logger.warning("MQTT write rejected: Unsafe path %s", rel_path)
            return

        if len(payload) > self.write_max_bytes:
            logger.warning("MQTT write rejected: Payload too large")
            return

        if not self._check_quota(len(payload)):
            logger.warning("MQTT write rejected: Quota exceeded")
            return

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(target_path, "wb") as f:
                await f.write(payload)
            logger.info("MQTT wrote file: %s", rel_path)
        except Exception as e:
            logger.error("Failed to write file %s via MQTT: %s", rel_path, e)

    async def _handle_mqtt_read(self, segments: list[str]) -> None:
        if not segments:
            return

        rel_path = "/".join(segments)
        target_path = self._get_safe_path(rel_path)

        if not target_path or not target_path.exists() or not target_path.is_file():
            return

        try:
            async with aiofiles.open(target_path, "rb") as f:
                content = await f.read()

            # Limit read size for MQTT
            if len(content) > self.write_max_bytes:
                content = content[:self.write_max_bytes]

            response_topic = f"{Topic.FILE}/read/{rel_path}"
            await self.publish_mqtt(response_topic, content, False)
        except Exception as e:
            logger.error("Failed to read file %s for MQTT: %s", rel_path, e)

    async def _handle_mqtt_remove(self, segments: list[str]) -> None:
        if not segments:
            return

        rel_path = "/".join(segments)
        target_path = self._get_safe_path(rel_path)

        if target_path and target_path.exists():
            try:
                if target_path.is_dir():
                    shutil.rmtree(target_path)
                else:
                    os.unlink(target_path)
                logger.info("MQTT removed: %s", rel_path)
            except Exception as e:
                logger.error("Failed to remove %s via MQTT: %s", rel_path, e)

    def _get_safe_path(self, relative_path: str) -> Path | None:
        """Sanitize and resolve path ensuring it stays within root."""
        # Remove leading slashes to treat as relative
        clean_rel = relative_path.lstrip("/\\")
        if not clean_rel:
            return None

        try:
            candidate = (self.root / clean_rel).resolve()
            if not str(candidate).startswith(str(self.root)):
                return None
            return candidate
        except Exception:
            return None

    def _check_quota(self, pending_bytes: int) -> bool:
        """Check if adding bytes would exceed quota."""
        if self.storage_quota_bytes <= 0:
            return True

        current_usage = self._calculate_usage()
        return (current_usage + pending_bytes) <= self.storage_quota_bytes

    def _calculate_usage(self) -> int:
        """Calculate total bytes used in root directory recursively."""
        total_size = 0
        try:
            # [OPTIMIZATION] Usamos os.scandir que es más rápido que os.walk
            total_size = self._scan_directory_usage(self.root)
        except Exception as e:
            logger.error("Error calculating storage usage: %s", e)
        return total_size

    def _scan_directory_usage(self, directory: Path) -> int:
        size = 0
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            size += entry.stat().st_size
                        elif entry.is_dir(follow_symlinks=False):
                            size += self._scan_directory_usage(Path(entry.path))
                    except (PermissionError, OSError) as e:
                        # [HARDENING] Silence permission errors during scan (common in /tmp)
                        logger.debug("Skipping %s due to permission/OS error: %s", entry.path, e)
                        continue
        except (PermissionError, OSError) as e:
            logger.debug("Cannot scan directory %s: %s", directory, e)
        return size
