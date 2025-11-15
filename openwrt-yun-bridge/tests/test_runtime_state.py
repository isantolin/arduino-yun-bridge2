"""Unit tests for RuntimeState helpers."""
from __future__ import annotations

from collections.abc import Iterator
import logging

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.state.context import RuntimeState


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def logger_spy() -> Iterator[tuple[logging.Logger, _ListHandler]]:
    logger = logging.getLogger("yunbridge.tests")
    handler = _ListHandler()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield logger, handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def test_enqueue_console_chunk_trims_and_drops(
    runtime_state: RuntimeState,
    logger_spy: tuple[logging.Logger, _ListHandler],
) -> None:
    logger, handler = logger_spy

    runtime_state.enqueue_console_chunk(b"a" * 128, logger)
    assert runtime_state.console_queue_bytes == 64
    assert runtime_state.console_to_mcu_queue[-1] == b"a" * 64

    runtime_state.enqueue_console_chunk(b"b" * 64, logger)
    assert runtime_state.console_queue_bytes == 64
    assert runtime_state.console_to_mcu_queue[-1] == b"b" * 64

    warnings = [record.getMessage() for record in handler.records]
    assert any(
        "Console chunk truncated" in message for message in warnings
    )
    assert any(
        "Dropping oldest console chunk" in message for message in warnings
    )


def test_enqueue_mailbox_message_respects_limits(
    runtime_state: RuntimeState,
    logger_spy: tuple[logging.Logger, _ListHandler],
) -> None:
    logger, handler = logger_spy

    assert runtime_state.enqueue_mailbox_message(b"a" * 16, logger) is True
    assert runtime_state.enqueue_mailbox_message(b"b" * 16, logger) is True
    assert runtime_state.mailbox_queue_bytes == 32

    # Next message should trigger eviction and be accepted after trimming
    assert runtime_state.enqueue_mailbox_message(b"c" * 40, logger) is True
    assert runtime_state.mailbox_queue_bytes == 32
    assert len(runtime_state.mailbox_queue) == 1
    assert runtime_state.mailbox_queue[-1] == b"c" * 32

    warnings = [record.getMessage() for record in handler.records]
    assert any(
        "Mailbox message truncated" in message for message in warnings
    )
    assert any(
        "Dropping oldest mailbox message" in message for message in warnings
    )


def test_enqueue_mailbox_incoming_respects_limits(
    runtime_state: RuntimeState,
    logger_spy: tuple[logging.Logger, _ListHandler],
) -> None:
    logger, handler = logger_spy

    assert runtime_state.enqueue_mailbox_incoming(b"x" * 16, logger) is True
    assert runtime_state.enqueue_mailbox_incoming(b"y" * 16, logger) is True
    assert runtime_state.mailbox_incoming_queue_bytes == 32

    assert runtime_state.enqueue_mailbox_incoming(b"z" * 40, logger) is True
    assert runtime_state.mailbox_incoming_queue_bytes == 32
    assert len(runtime_state.mailbox_incoming_queue) == 1
    assert runtime_state.mailbox_incoming_queue[-1] == b"z" * 32

    warnings = [record.getMessage() for record in handler.records]
    assert any(
        "Mailbox incoming message truncated" in message
        for message in warnings
    )
    assert any(
        "Dropping oldest mailbox incoming message" in message
        for message in warnings
    )


def test_requeue_console_chunk_front_restores_bytes(
    runtime_state: RuntimeState,
) -> None:
    runtime_state.enqueue_console_chunk(b"hello", logging.getLogger())
    queued = runtime_state.pop_console_chunk()
    assert runtime_state.console_queue_bytes == 0

    runtime_state.requeue_console_chunk_front(queued)

    assert runtime_state.console_queue_bytes == len(queued)
    assert runtime_state.console_to_mcu_queue[0] == queued


def test_mqtt_queue_respects_config(
    runtime_state: RuntimeState,
    runtime_config: RuntimeConfig,
) -> None:
    assert (
        runtime_state.mqtt_publish_queue.maxsize
        == runtime_config.mqtt_queue_limit
    )
    assert runtime_state.mqtt_queue_limit == runtime_config.mqtt_queue_limit
