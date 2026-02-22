"""Filesystem component wrapping MCU and MQTT file operations."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from os import scandir
from pathlib import Path, PurePosixPath

from aiomqtt.message import Message
from construct import ConstructError
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, FileAction, Status

from ..config.const import (
    FILE_LARGE_WARNING_BYTES,
    MQTT_EXPIRY_SHELL,
    MQTT_USER_PROP_FILE_PATH,
    SYSTEMD_PRIVATE_PREFIX,
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
from ..protocol.topics import Topic, split_topic_segments, topic_path
from ..state.context import RuntimeState
from ..util import chunk_bytes
from .base import BridgeContext

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
        try:
            packet = FileWritePacket.decode(payload)
        except (ConstructError, ValueError) as e:
            logger.warning("Invalid FileWrite payload: %s", e)
            await self.ctx.send_frame(
                Status.MALFORMED.value,
                encode_status_reason(protocol.STATUS_REASON_COMMAND_VALIDATION_FAILED),
            )
            return False

        path = packet.path

        # [SECURITY 10/10] Path Traversal Protection (Hardening)
        posix_path = PurePosixPath(path)
        if ".." in posix_path.parts or posix_path.is_absolute():
            logger.warning("Security Alert: Dangerous path blocked: %s", path)
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason(protocol.STATUS_REASON_INVALID_PATH),
            )
            return False

        success, _, reason = await self._perform_file_operation(FileAction.WRITE, path, packet.data)
        if success:
            await self.ctx.send_frame(Status.OK.value, b"")
            return True

        await self.ctx.send_frame(
            Status.ERROR.value,
            encode_status_reason(reason or protocol.STATUS_REASON_WRITE_FAILED),
        )
        return False

    async def handle_read(self, payload: bytes) -> None:
        try:
            packet = FileReadPacket.decode(payload)
        except (ConstructError, ValueError) as e:
            logger.warning("Invalid FileRead payload: %s", e)
            await self.ctx.send_frame(Status.MALFORMED.value, b"")
            return

        success, content, reason = await self._perform_file_operation(FileAction.READ, packet.path)

        if not success:
            await self.ctx.send_frame(
                Status.ERROR.value,
                encode_status_reason(reason or protocol.STATUS_REASON_READ_FAILED),
            )
            return

        data = content or b""
        # Overhead: 2 bytes for Prefixed length in FileReadResponsePacket schema
        max_chunk = protocol.MAX_PAYLOAD_SIZE - 2

        chunks = chunk_bytes(data, max_chunk)
        if not chunks:
            response = FileReadResponsePacket(content=b"").encode()
            await self.ctx.send_frame(Command.CMD_FILE_READ_RESP.value, response)
            return

        for chunk in chunks:
            response = FileReadResponsePacket(content=chunk).encode()
            await self.ctx.send_frame(Command.CMD_FILE_READ_RESP.value, response)

    async def handle_remove(self, payload: bytes) -> bool:
        try:
            packet = FileRemovePacket.decode(payload)
        except (ConstructError, ValueError) as e:
            logger.warning("Invalid FileRemove payload: %s", e)
            await self.ctx.send_frame(Status.MALFORMED.value, b"")
            return False

        success, _, reason = await self._perform_file_operation(FileAction.REMOVE, packet.path)
        if success:
            await self.ctx.send_frame(Status.OK.value, b"")
            return True

        await self.ctx.send_frame(
            Status.ERROR.value,
            encode_status_reason(reason or protocol.STATUS_REASON_REMOVE_FAILED),
        )
        return False

    async def handle_mqtt(
        self,
        action: str,
        path_parts: list[str],
        payload: bytes,
        inbound: Message | None = None,
    ) -> None:
        filename = "/".join(path_parts)
        if not filename:
            logger.warning("MQTT file action missing filename for %s", action)
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
            if action == FileAction.WRITE:
                await self._handle_mqtt_write(filename, payload, outcome)
                return
            if action == FileAction.READ:
                await self._handle_mqtt_read(filename, inbound, outcome)
                return
            if action == FileAction.REMOVE:
                await self._handle_mqtt_remove(filename, outcome)
                return

            logger.debug("Ignoring file action '%s'", action)

    async def _handle_mqtt_write(
        self,
        filename: str,
        payload: bytes,
        outcome: dict[str, str],
    ) -> None:
        success, _, reason = await self._perform_file_operation(FileAction.WRITE, filename, payload)
        if not success:
            outcome["status"] = reason or "write_failed"
            logger.error(
                "MQTT file write failed for %s: %s",
                filename,
                reason or "unknown_reason",
            )
        else:
            outcome["status"] = "ok"

    async def _handle_mqtt_read(
        self,
        filename: str,
        inbound: Message | None,
        outcome: dict[str, str],
    ) -> None:
        success, content, reason = await self._perform_file_operation(
            FileAction.READ,
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
            FileAction.READ,
            protocol.MQTT_SUFFIX_RESPONSE,
            *split_topic_segments(filename),
        )
        await self.ctx.publish(
            topic=response_topic,
            payload=data,
            expiry=MQTT_EXPIRY_SHELL,
            properties=((MQTT_USER_PROP_FILE_PATH, filename),),
            reply_to=inbound,
        )

    async def _handle_mqtt_remove(
        self,
        filename: str,
        outcome: dict[str, str],
    ) -> None:
        success, _, reason = await self._perform_file_operation(FileAction.REMOVE, filename)
        if not success:
            outcome["status"] = reason or "remove_failed"
            logger.error(
                "MQTT file remove failed for %s: %s",
                filename,
                reason or "unknown_reason",
            )
        else:
            outcome["status"] = "ok"

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
            match operation:
                case FileAction.WRITE:
                    assert data is not None
                    return await self._write_with_quota(safe_path, data)

                case FileAction.READ:
                    content = await asyncio.to_thread(safe_path.read_bytes)
                    logger.info("Read %d bytes from %s", len(content), safe_path)
                    return True, content, "ok"

                case FileAction.REMOVE:
                    return await self._remove_with_tracking(safe_path)

                case _:
                    return False, None, "unknown_operation"

        except OSError as exc:
            logger.exception(
                "File operation %s failed for %s",
                operation,
                filename,
            )
            return False, None, str(exc)

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
                ("Path traversal blocked. filename='%s', " "resolved='%s', base='%s'"),
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
        # [FLASH PROTECTION]
        # Warn if writing to potentially non-volatile storage (not /tmp or /mnt)
        try:
            resolved = path.resolve()
            is_volatile = False
            for safe_prefix in VOLATILE_STORAGE_PATHS:
                if str(resolved).startswith(safe_prefix):
                    is_volatile = True
                    break

            if not is_volatile:
                logger.warning(
                    "FLASH WEAR WARNING: Writing to non-volatile storage: %s. "
                    "This may damage the device flash memory. Use /tmp or /mnt.",
                    resolved,
                )
        except OSError as e:
            # Don't block write if check fails, but log specific OS errors
            logger.debug("Failed to verify flash write safety: %s", e, exc_info=True)
            pass

        payload_size = len(data)
        async with self._storage_lock:
            limit = max(1, self.state.file_write_max_bytes)
            if payload_size > limit:
                self.state.file_write_limit_rejections += 1
                logger.warning(
                    ("Rejecting %d-byte file write to %s: exceeds " "per-write limit of %d byte(s)."),
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
                    ("Rejecting file write to %s: projected usage %d " "byte(s) exceeds quota of %d byte(s)."),
                    path,
                    projected_usage,
                    quota,
                )
                return False, None, "storage_quota_exceeded"

            try:
                await asyncio.to_thread(_do_write_file, path, data)
            except OSError as exc:
                logger.error("Failed to write file %s: %s", path, exc)
                return False, None, str(exc)

            self.state.file_storage_bytes_used = projected_usage
            logger.info("Wrote %d bytes to %s", payload_size, path)
            return True, None, "ok"

    async def _remove_with_tracking(
        self,
        path: Path,
    ) -> tuple[bool, bytes | None, str | None]:
        async with self._storage_lock:
            removed_bytes = self._existing_file_size(path)
            try:
                await asyncio.to_thread(path.unlink)
            except OSError as exc:
                logger.error("Failed to remove file %s: %s", path, exc)
                return False, None, str(exc)

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
    def _scan_directory_size(root: Path, max_depth: int = 10) -> int:
        total = 0
        # Stack stores (path, depth) tuples
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > max_depth:
                continue

            try:
                with scandir(current) as iterator:
                    for entry in iterator:
                        if entry.is_symlink():
                            continue
                        if current == Path("/tmp") and entry.name.startswith(SYSTEMD_PRIVATE_PREFIX):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append((Path(entry.path), depth + 1))
                                continue
                            if entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
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
            resolved = base_dir.resolve()
        except OSError:
            resolved = base_dir.absolute()

        if not self.state.allow_non_tmp_paths:
            resolved_str = str(resolved)
            if resolved_str != "/tmp" and not resolved_str.startswith("/tmp/"):
                logger.warning(
                    "FLASH PROTECTION: Rejecting file_system_root outside /tmp: %s",
                    resolved,
                )
                return None

        try:
            base_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("Failed to create base directory for files: %s", base_dir)
            return None

        return resolved

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


__all__ = ["FileComponent"]
