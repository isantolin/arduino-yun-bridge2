"""Runtime state container for the Yun Bridge daemon."""
from __future__ import annotations

import asyncio
import collections
import logging
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from ..mqtt import PublishableMessage

from ..config.settings import RuntimeConfig


def _mqtt_queue_factory() -> asyncio.Queue[PublishableMessage]:
    return asyncio.Queue()


def _bytes_deque_factory() -> Deque[bytes]:
    return collections.deque()


def _int_deque_factory() -> Deque[int]:
    return collections.deque()


def _str_deque_factory() -> Deque[str]:
    return collections.deque()


def _str_dict_factory() -> Dict[str, str]:
    return {}


def _command_list_factory() -> List[str]:
    return []


def _str_int_dict_factory() -> Dict[str, int]:
    return {}


def _process_dict_factory() -> Dict[int, asyncio.subprocess.Process]:
    return {}


def _buffer_dict_factory() -> Dict[int, bytearray]:
    return {}


def _exit_code_dict_factory() -> Dict[int, int]:
    return {}


STATUS_FILE_PATH: str = "/tmp/yunbridge_status.json"


@dataclass
class RuntimeState:
    """Aggregated mutable state shared across the daemon layers."""

    serial_writer: Optional[asyncio.StreamWriter] = None
    mqtt_publish_queue: asyncio.Queue[PublishableMessage] = field(
        default_factory=_mqtt_queue_factory
    )
    mqtt_queue_limit: int = 256
    mqtt_dropped_messages: int = 0
    mqtt_drop_counts: Dict[str, int] = field(
        default_factory=_str_int_dict_factory
    )
    datastore: Dict[str, str] = field(default_factory=_str_dict_factory)
    mailbox_queue: Deque[bytes] = field(default_factory=_bytes_deque_factory)
    mcu_is_paused: bool = False
    console_to_mcu_queue: Deque[bytes] = field(
        default_factory=_bytes_deque_factory
    )
    console_queue_limit_bytes: int = 16384
    console_queue_bytes: int = 0
    running_processes: Dict[int, asyncio.subprocess.Process] = field(
        default_factory=_process_dict_factory
    )
    process_stdout_buffer: Dict[int, bytearray] = field(
        default_factory=_buffer_dict_factory
    )
    process_stderr_buffer: Dict[int, bytearray] = field(
        default_factory=_buffer_dict_factory
    )
    process_exit_codes: Dict[int, int] = field(
        default_factory=_exit_code_dict_factory
    )
    process_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_pid: int = 1
    allowed_commands: list[str] = field(default_factory=_command_list_factory)
    process_timeout: int = 10
    file_system_root: str = "/root/yun_files"
    mqtt_topic_prefix: str = "br"
    pending_digital_reads: Deque[int] = field(
        default_factory=_int_deque_factory
    )
    pending_analog_reads: Deque[int] = field(
        default_factory=_int_deque_factory
    )
    pending_datastore_gets: Deque[str] = field(
        default_factory=_str_deque_factory
    )
    mailbox_incoming_topic: str = ""
    mailbox_queue_limit: int = 64
    mailbox_queue_bytes_limit: int = 65536
    mailbox_queue_bytes: int = 0
    mailbox_incoming_queue: Deque[bytes] = field(
        default_factory=_bytes_deque_factory
    )
    mailbox_incoming_queue_bytes: int = 0
    mcu_version: Optional[tuple[int, int]] = None
    link_handshake_nonce: Optional[bytes] = None
    link_is_synchronized: bool = False

    def configure(self, config: RuntimeConfig) -> None:
        self.allowed_commands = list(config.allowed_commands)
        self.process_timeout = config.process_timeout
        self.file_system_root = config.file_system_root
        self.mqtt_topic_prefix = config.mqtt_topic
        self.console_queue_limit_bytes = config.console_queue_limit_bytes
        self.mailbox_queue_limit = config.mailbox_queue_limit
        self.mailbox_queue_bytes_limit = config.mailbox_queue_bytes_limit
        self.mqtt_queue_limit = config.mqtt_queue_limit

    def enqueue_console_chunk(
        self, chunk: bytes, logger: logging.Logger
    ) -> None:
        if not chunk:
            return
        data = bytes(chunk)
        chunk_len = len(data)
        if chunk_len > self.console_queue_limit_bytes:
            logger.warning(
                "Console chunk truncated from %d to %d bytes to respect "
                "limit.",
                chunk_len,
                self.console_queue_limit_bytes,
            )
            data = data[-self.console_queue_limit_bytes:]
            chunk_len = len(data)

        while (
            self.console_queue_bytes + chunk_len
            > self.console_queue_limit_bytes
            and self.console_to_mcu_queue
        ):
            removed = self.console_to_mcu_queue.popleft()
            self.console_queue_bytes -= len(removed)
            logger.warning(
                "Dropping oldest console chunk (%d bytes) due to buffer "
                "limit.",
                len(removed),
            )

        if (
            self.console_queue_bytes + chunk_len
            > self.console_queue_limit_bytes
        ):
            logger.error(
                "Console queue overflow; dropping %d-byte chunk after "
                "trimming.",
                chunk_len,
            )
            return

        self.console_to_mcu_queue.append(data)
        self.console_queue_bytes += chunk_len

    def pop_console_chunk(self) -> bytes:
        chunk = self.console_to_mcu_queue.popleft()
        self.console_queue_bytes -= len(chunk)
        return chunk

    def requeue_console_chunk_front(self, chunk: bytes) -> None:
        if not chunk:
            return

        chunk_len = len(chunk)
        # The caller should ensure the chunk fits within the configured limit.
        if chunk_len > self.console_queue_limit_bytes:
            # Truncate to respect the limit; this situation should be rare and
            # indicates the configured limit is smaller than incoming frames.
            data = bytes(chunk[-self.console_queue_limit_bytes:])
            chunk_len = len(data)
        else:
            data = bytes(chunk)

        self.console_to_mcu_queue.appendleft(data)
        self.console_queue_bytes += chunk_len

    def enqueue_mailbox_message(
        self, payload: bytes, logger: logging.Logger
    ) -> bool:
        data = bytes(payload)
        length = len(data)
        if length > self.mailbox_queue_bytes_limit:
            logger.warning(
                "Mailbox message truncated from %d to %d bytes to respect "
                "limit.",
                length,
                self.mailbox_queue_bytes_limit,
            )
            data = data[: self.mailbox_queue_bytes_limit]
            length = len(data)

        while (
            (
                len(self.mailbox_queue) >= self.mailbox_queue_limit
                or (
                    self.mailbox_queue_bytes + length
                    > self.mailbox_queue_bytes_limit
                )
            )
        ) and self.mailbox_queue:
            removed = self.mailbox_queue.popleft()
            self.mailbox_queue_bytes -= len(removed)
            logger.warning(
                "Dropping oldest mailbox message (%d bytes) to honor limits.",
                len(removed),
            )

        if (
            len(self.mailbox_queue) >= self.mailbox_queue_limit
            or (
                self.mailbox_queue_bytes + length
                > self.mailbox_queue_bytes_limit
            )
        ):
            logger.error(
                "Mailbox queue overflow; rejecting incoming message (%d "
                "bytes).",
                length,
            )
            return False

        self.mailbox_queue.append(data)
        self.mailbox_queue_bytes += length
        return True

    def pop_mailbox_message(self) -> Optional[bytes]:
        if not self.mailbox_queue:
            return None
        message = self.mailbox_queue.popleft()
        self.mailbox_queue_bytes -= len(message)
        return message

    def requeue_mailbox_message_front(self, payload: bytes) -> None:
        data = bytes(payload)
        self.mailbox_queue.appendleft(data)
        self.mailbox_queue_bytes += len(data)

    def enqueue_mailbox_incoming(
        self, payload: bytes, logger: logging.Logger
    ) -> bool:
        data = bytes(payload)
        length = len(data)
        if length > self.mailbox_queue_bytes_limit:
            logger.warning(
                "Mailbox incoming message truncated from %d to %d bytes to "
                "respect limit.",
                length,
                self.mailbox_queue_bytes_limit,
            )
            data = data[: self.mailbox_queue_bytes_limit]
            length = len(data)

        while (
            (
                len(self.mailbox_incoming_queue) >= self.mailbox_queue_limit
                or (
                    self.mailbox_incoming_queue_bytes + length
                    > self.mailbox_queue_bytes_limit
                )
            )
            and self.mailbox_incoming_queue
        ):
            removed = self.mailbox_incoming_queue.popleft()
            self.mailbox_incoming_queue_bytes -= len(removed)
            logger.warning(
                "Dropping oldest mailbox incoming message (%d bytes) to "
                "honor limits.",
                len(removed),
            )

        if (
            len(self.mailbox_incoming_queue) >= self.mailbox_queue_limit
            or (
                self.mailbox_incoming_queue_bytes + length
                > self.mailbox_queue_bytes_limit
            )
        ):
            logger.error(
                "Mailbox incoming queue overflow; rejecting message (%d "
                "bytes).",
                length,
            )
            return False

        self.mailbox_incoming_queue.append(data)
        self.mailbox_incoming_queue_bytes += length
        return True

    def pop_mailbox_incoming(self) -> Optional[bytes]:
        if not self.mailbox_incoming_queue:
            return None
        message = self.mailbox_incoming_queue.popleft()
        self.mailbox_incoming_queue_bytes -= len(message)
        return message

    def record_mqtt_drop(self, topic: str) -> None:
        self.mqtt_dropped_messages += 1
        self.mqtt_drop_counts[topic] = self.mqtt_drop_counts.get(topic, 0) + 1


def create_runtime_state(config: RuntimeConfig) -> RuntimeState:
    state = RuntimeState(
        mqtt_publish_queue=asyncio.Queue(config.mqtt_queue_limit),
        mqtt_queue_limit=config.mqtt_queue_limit,
    )
    state.configure(config)
    return state
