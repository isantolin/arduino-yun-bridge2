"""Filesystem component wrapping MCU and MQTT file operations."""
from __future__ import annotations

import asyncio
import logging
import os
import struct
from typing import Optional, Tuple

from yunbridge.rpc.protocol import Command, MAX_PAYLOAD_SIZE, Status

from ...common import encode_status_reason
from ...const import TOPIC_FILE
from ...mqtt import InboundMessage, PublishableMessage
from ...config.settings import RuntimeConfig
from ...state.context import RuntimeState
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
            "write", path, file_data
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
            "read", filename
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
        response = struct.pack(">H", len(data)) + data
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
            "remove", filename
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
        inbound: Optional[InboundMessage] = None,
    ) -> None:
        filename = "/".join(path_parts)
        if not filename:
            logger.warning(
                "MQTT file action missing filename for %s", action
            )
            return

        if action == "write":
            success, _, reason = await self._perform_file_operation(
                "write", filename, payload
            )
            if not success:
                logger.error(
                    "MQTT file write failed for %s: %s",
                    filename,
                    reason or "unknown_reason",
                )
        elif action == "read":
            success, content, reason = await self._perform_file_operation(
                "read", filename
            )
            if not success:
                logger.error(
                    "MQTT file read failed for %s: %s",
                    filename,
                    reason or "unknown_reason",
                )
                return
            data = content or b""
            response_topic = (
                f"{self.state.mqtt_topic_prefix}/{TOPIC_FILE}/read/response/"
                f"{filename}"
            )
            message = (
                PublishableMessage(
                    topic_name=response_topic,
                    payload=data,
                )
                .with_message_expiry(30)
                .with_user_property("bridge-file-path", filename)
            )

            await self.ctx.enqueue_mqtt(
                message,
                reply_context=inbound,
            )
        elif action == "remove":
            success, _, reason = await self._perform_file_operation(
                "remove", filename
            )
            if not success:
                logger.error(
                    "MQTT file remove failed for %s: %s",
                    filename,
                    reason or "unknown_reason",
                )
        else:
            logger.debug("Ignoring unknown file action '%s'", action)

    async def _perform_file_operation(
        self,
        operation: str,
        filename: str,
        data: Optional[bytes] = None,
    ) -> Tuple[bool, Optional[bytes], Optional[str]]:
        safe_path = await self._get_safe_path(filename)
        if not safe_path:
            logger.warning(
                "File operation rejected due to unsafe path: %s",
                filename,
            )
            return False, None, "unsafe_path"

        try:
            if operation == "write":
                assert data is not None
                await asyncio.to_thread(self._write_file_sync, safe_path, data)
                logger.info("Wrote %d bytes to %s", len(data), safe_path)
                return True, None, "ok"

            if operation == "read":
                content = await asyncio.to_thread(
                    self._read_file_sync, safe_path
                )
                logger.info("Read %d bytes from %s", len(content), safe_path)
                return True, content, "ok"

            if operation == "remove":
                await asyncio.to_thread(os.remove, safe_path)
                logger.info("Removed file %s", safe_path)
                return True, None, "ok"

        except OSError as exc:
            logger.exception(
                "File operation %s failed for %s",
                operation,
                filename,
            )
            return False, None, str(exc)
        return False, None, "unknown_operation"

    async def _get_safe_path(self, filename: str) -> Optional[str]:
        base_dir = os.path.abspath(self.state.file_system_root)
        try:
            os.makedirs(base_dir, exist_ok=True)
        except OSError:
            logger.exception(
                "Failed to create base directory for files: %s", base_dir
            )
            return None

        cleaned_filename = filename.lstrip("./\\").replace("../", "")
        safe_path = os.path.abspath(os.path.join(base_dir, cleaned_filename))

        if os.path.commonpath([safe_path, base_dir]) != base_dir:
            logger.warning(
                (
                    "Path traversal blocked. filename='%s', "
                    "resolved='%s', base='%s'"
                ),
                filename,
                safe_path,
                base_dir,
            )
            return None
        return safe_path

    @staticmethod
    def _write_file_sync(path: str, data: bytes) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    @staticmethod
    def _read_file_sync(path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


__all__ = ["FileComponent"]
