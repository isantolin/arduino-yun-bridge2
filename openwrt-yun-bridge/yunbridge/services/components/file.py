"""Filesystem component wrapping MCU and MQTT file operations."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path, PurePosixPath

from aiomqtt.message import Message as MQTTMessage
from yunbridge.rpc.protocol import Command, MAX_PAYLOAD_SIZE, Status
from yunbridge.const import (
    ACTION_FILE_READ,
    ACTION_FILE_WRITE,
    ACTION_FILE_REMOVE,
)

from ...common import encode_status_reason, pack_u16
from ...mqtt.messages import QueuedPublish
from ...config.settings import RuntimeConfig
from ...state.context import RuntimeState
from ...protocol.topics import Topic, topic_path
from .base import BridgeContext

logger = logging.getLogger("yunbridge.file")


class FileComponent:
    """Encapsulate file read/write/remove logic."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        ctx: BridgeContext,
    ) -> None:
        self.config = config
        self.state = state
        self.ctx = ctx
        self._storage_lock = asyncio.Lock()
        self._usage_seeded = False
        self._ensure_usage_seeded()

    async def handle_write(self, payload: bytes) -> bool:
        if len(payload) < 3:
            logger.warning(
                "Invalid file write payload length: %d", len(payload)
            )
            return False

        path_len = payload[0]
        cursor = 1
        if len(payload) < cursor + path_len + 2:
            logger.warning(
                "Invalid file write payload: missing data section"
            )
            return False

        path = payload[cursor:cursor + path_len].decode(
            "utf-8", errors="ignore"
        )
        cursor += path_len
        data_len = int.from_bytes(payload[cursor:cursor + 2], "big")
        cursor += 2

        file_data = payload[cursor:cursor + data_len]
        if len(file_data) != data_len:
            logger.warning(
                "File write payload truncated. Expected %d bytes.", data_len
            )
            return False

        success, _, reason = await self._perform_file_operation(
            ACTION_FILE_WRITE, path, file_data
        )
        if success:
            return True

        await self.ctx.send_frame(
            Status.ERROR.value,
            encode_status_reason(reason or "write_failed"),
        )
        return False

    async def handle_read(self, payload: bytes) -> None:
        if len(payload) < 1:
            logger.warning(
                "Invalid file read payload length: %d", len(payload)
            )
            return

        path_len = payload[0]
        if len(payload) < 1 + path_len:
            logger.warning("Invalid file read payload: missing path bytes")
            return

        filename = payload[1:1 + path_len].decode("utf-8", errors="ignore")
        success, content, reason = await self._perform_file_operation(
            ACTION_FILE_READ, filename
        )

        if not success:
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason(reason or "read_failed"),
            )
            return

        data = content or b""
        max_payload = MAX_PAYLOAD_SIZE - 2
        if len(data) > max_payload:
            logger.warning(
                "File read response truncated from %d to %d bytes for %s",
                len(data),
                max_payload,
                filename,
            )
            data = data[:max_payload]
        response = pack_u16(len(data)) + data
        await self.ctx.send_frame(Command.CMD_FILE_READ_RESP.value, response)

    async def handle_remove(self, payload: bytes) -> bool:
        if len(payload) < 1:
            logger.warning(
                "Invalid file remove payload length: %d", len(payload)
            )
            return False

        path_len = payload[0]
        if len(payload) < 1 + path_len:
            logger.warning("Invalid file remove payload: missing path bytes")
            return False

        filename = payload[1:1 + path_len].decode("utf-8", errors="ignore")
        success, _, reason = await self._perform_file_operation(
            ACTION_FILE_REMOVE, filename
        )
        if success:
            return True

        await self.ctx.send_frame(
            Status.ERROR.value,
            encode_status_reason(reason or "remove_failed"),
        )
        return False

    async def handle_mqtt(
        self,
        action: str,
        path_parts: list[str],
        payload: bytes,
        inbound: MQTTMessage | None = None,
    ) -> None:
        filename = "/".join(path_parts)
        if not filename:
            logger.warning(
                "MQTT file action missing filename for %s", action
            )
            return

        outcome: dict[str, str] = {"status": "ignored"}
        action_label = action or "<missing>"
        async with AsyncExitStack() as stack:
            stack.callback(
                self._log_mqtt_outcome,
                action_label,
                filename,
                outcome,
            )
            if action == ACTION_FILE_WRITE:
                success, _, reason = await self._perform_file_operation(
                    ACTION_FILE_WRITE, filename, payload
                )
                if not success:
                    outcome["status"] = reason or "write_failed"
                    logger.error(
                        "MQTT file write failed for %s: %s",
                        filename,
                        reason or "unknown_reason",
                    )
                else:
                    outcome["status"] = "ok"
            elif action == ACTION_FILE_READ:
                (
                    success,
                    content,
                    reason,
                ) = await self._perform_file_operation(
                    ACTION_FILE_READ,
                    filename,
                )
                if not success:
                    outcome["status"] = reason or "read_failed"
                    logger.error(
                        "MQTT file read failed for %s: %s",
                        filename,
                        reason or "unknown_reason",
                    )
                    return
                outcome["status"] = "ok"
                data = content or b""
                response_topic = topic_path(
                    self.state.mqtt_topic_prefix,
                    Topic.FILE,
                    ACTION_FILE_READ,
                    "response",
                    *tuple(
                        segment
                        for segment in filename.split("/")
                        if segment
                    ),
                )
                message = QueuedPublish(
                    topic_name=response_topic,
                    payload=data,
                    message_expiry_interval=30,
                    user_properties=(("bridge-file-path", filename),),
                )

                await self.ctx.enqueue_mqtt(
                    message,
                    reply_context=inbound,
                )
            elif action == ACTION_FILE_REMOVE:
                success, _, reason = await self._perform_file_operation(
                    ACTION_FILE_REMOVE, filename
                )
                if not success:
                    outcome["status"] = reason or "remove_failed"
                    logger.error(
                        "MQTT file remove failed for %s: %s",
                        filename,
                        reason or "unknown_reason",
                    )
                else:
                    outcome["status"] = "ok"
            else:
                logger.debug("Ignoring unknown file action '%s'", action)

    @staticmethod
    def _log_mqtt_outcome(
        action: str,
        filename: str,
        outcome: dict[str, str],
    ) -> None:
        logger.debug(
            "MQTT file action '%s' for %s finished with %s",
            action,
            filename or "<missing>",
            outcome.get("status", "unknown"),
        )

    async def _perform_file_operation(
        self,
        operation: str,
        filename: str,
        data: bytes | None = None,
    ) -> tuple[bool, bytes | None, str | None]:
        safe_path = self._get_safe_path(filename)
        if not safe_path:
            logger.warning(
                "File operation rejected due to unsafe path: %s",
                filename,
            )
            return False, None, "unsafe_path"

        self._ensure_usage_seeded()

        try:
            if operation == ACTION_FILE_WRITE:
                assert data is not None
                return await self._write_with_quota(safe_path, data)

            if operation == ACTION_FILE_READ:
                content = await asyncio.to_thread(
                    self._read_file_sync, safe_path
                )
                logger.info("Read %d bytes from %s", len(content), safe_path)
                return True, content, "ok"

            if operation == ACTION_FILE_REMOVE:
                return await self._remove_with_tracking(safe_path)

        except OSError as exc:
            logger.exception(
                "File operation %s failed for %s",
                operation,
                filename,
            )
            return False, None, str(exc)
        return False, None, "unknown_operation"

    def _get_safe_path(self, filename: str) -> Path | None:
        base_dir = self._get_base_dir()
        if base_dir is None:
            return None

        normalised = self._normalise_filename(filename)
        if normalised is None:
            logger.warning("Rejected unsafe filename '%s'", filename)
            return None

        candidate = base_dir.joinpath(*normalised.parts)
        try:
            safe_path = candidate.resolve()
            safe_path.relative_to(base_dir)
        except (OSError, ValueError):
            logger.warning(
                (
                    "Path traversal blocked. filename='%s', "
                    "resolved='%s', base='%s'"
                ),
                filename,
                candidate,
                base_dir,
            )
            return None
        return safe_path

    @staticmethod
    def _normalise_filename(filename: str) -> PurePosixPath | None:
        stripped = filename.replace("\\", "/").strip()
        if not stripped:
            return None

        try:
            posix_path = PurePosixPath(stripped)
        except ValueError:
            return None

        if posix_path.is_absolute():
            try:
                posix_path = posix_path.relative_to("/")
            except ValueError:
                return None

        cleaned_parts: list[str] = []
        for part in posix_path.parts:
            if part in {"", "."}:
                continue
            if part == ".." or "\x00" in part:
                return None
            cleaned_parts.append(part)

        if not cleaned_parts:
            return None

        return PurePosixPath(*cleaned_parts)

    async def _write_with_quota(
        self,
        path: Path,
        data: bytes,
    ) -> tuple[bool, bytes | None, str | None]:
        payload_size = len(data)
        async with self._storage_lock:
            limit = max(1, self.state.file_write_max_bytes)
            if payload_size > limit:
                self.state.file_write_limit_rejections += 1
                logger.warning(
                    (
                        "Rejecting %d-byte file write to %s: exceeds "
                        "per-write limit of %d byte(s)."
                    ),
                    payload_size,
                    path,
                    limit,
                )
                return False, None, "write_limit_exceeded"

            current_usage = self.state.file_storage_bytes_used
            previous_size = self._existing_file_size(path)
            if previous_size > current_usage:
                current_usage = self._refresh_storage_usage()
                previous_size = min(previous_size, current_usage)

            projected_usage = current_usage - previous_size + payload_size
            quota = max(limit, self.state.file_storage_quota_bytes)
            if projected_usage > quota:
                self.state.file_storage_limit_rejections += 1
                logger.warning(
                    (
                        "Rejecting file write to %s: projected usage %d "
                        "byte(s) exceeds quota of %d byte(s)."
                    ),
                    path,
                    projected_usage,
                    quota,
                )
                return False, None, "storage_quota_exceeded"

            await asyncio.to_thread(self._write_file_sync, path, data)
            self.state.file_storage_bytes_used = projected_usage
            logger.info("Wrote %d bytes to %s", payload_size, path)
            return True, None, "ok"

    async def _remove_with_tracking(
        self,
        path: Path,
    ) -> tuple[bool, bytes | None, str | None]:
        async with self._storage_lock:
            removed_bytes = self._existing_file_size(path)
            await asyncio.to_thread(path.unlink)
            self._decrement_storage_usage(removed_bytes)
            logger.info("Removed file %s", path)
            return True, None, "ok"

    def _ensure_usage_seeded(self) -> None:
        if self._usage_seeded:
            return
        self._refresh_storage_usage()
        self._usage_seeded = True

    def _refresh_storage_usage(self) -> int:
        base_dir = self._get_base_dir()
        if base_dir is None:
            self.state.file_storage_bytes_used = 0
            return 0
        usage = self._scan_directory_size(base_dir)
        self.state.file_storage_bytes_used = max(0, usage)
        return self.state.file_storage_bytes_used

    @staticmethod
    def _scan_directory_size(root: Path) -> int:
        total = 0
        stack: list[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    for entry in iterator:
                        if entry.is_symlink():
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                                continue
                            if entry.is_file(follow_symlinks=False):
                                total += entry.stat(
                                    follow_symlinks=False
                                ).st_size
                        except OSError as exc:
                            logger.debug(
                                "Failed to inspect %s during quota scan: %s",
                                entry.path,
                                exc,
                            )
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning(
                    "Unable to scan %s for quota tracking: %s",
                    current,
                    exc,
                )
        return total

    def _get_base_dir(self) -> Path | None:
        base_dir = Path(self.state.file_system_root).expanduser()
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception(
                "Failed to create base directory for files: %s", base_dir
            )
            return None
        try:
            return base_dir.resolve()
        except OSError:
            logger.exception(
                "Failed to resolve base directory for files: %s", base_dir
            )
            return None

    @staticmethod
    def _existing_file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def _decrement_storage_usage(self, bytes_removed: int) -> None:
        if bytes_removed <= 0:
            return
        remaining = self.state.file_storage_bytes_used - bytes_removed
        self.state.file_storage_bytes_used = max(0, remaining)

    @staticmethod
    def _write_file_sync(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def _read_file_sync(path: Path) -> bytes:
        return path.read_bytes()


__all__ = ["FileComponent"]
