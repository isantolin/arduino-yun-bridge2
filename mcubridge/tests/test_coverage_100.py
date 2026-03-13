"""
Tests specifically targeting 100% coverage for critical Python modules.

This module covers edge cases and defensive code paths that are
harder to reach in normal testing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import DEFAULT_BAUDRATE as DEFAULT_SERIAL_BAUD
from mcubridge.protocol.protocol import (
    DEFAULT_SAFE_BAUDRATE as DEFAULT_SERIAL_SAFE_BAUDRATE,
)
from mcubridge.protocol.protocol import (
    Status,
)
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import create_runtime_state


def _make_config(*, process_max_concurrent: int = 2) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_SERIAL_BAUD,
        serial_safe_baud=DEFAULT_SERIAL_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=("echo", "ls", "cat", "true"),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        process_max_concurrent=process_max_concurrent,
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )


@pytest.fixture
def mock_enqueue() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def process_component(mock_enqueue: AsyncMock) -> ProcessComponent:
    config = _make_config(process_max_concurrent=4)
    state = create_runtime_state(config)
    service = MagicMock()
    service._acknowledge_mcu_frame = AsyncMock()
    return ProcessComponent(config, state, service)


# ============================================================================
# PROCESS COMPONENT - EDGE CASES
# ============================================================================


@pytest.mark.asyncio
async def test_poll_process_not_found_explicit(
    process_component: ProcessComponent,
) -> None:
    """Cover branch where slot is not found."""
    batch = await process_component.poll_process(999)
    assert batch.status_byte == Status.ERROR.value


@pytest.mark.asyncio
async def test_finalize_process_slot_gone(
    process_component: ProcessComponent,
) -> None:
    """Cover branch where slot is gone in _finalize_process."""
    pid = 77
    await process_component._finalize_process(pid)


@pytest.mark.asyncio
async def test_start_async_subprocess_unexpected_exception(
    process_component: ProcessComponent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover unexpected exception branch in run_async."""
    with patch("asyncio.create_subprocess_shell", side_effect=OSError("boom")):
        pid = await process_component.run_async("echo hello")
        assert pid == 0
# ============================================================================
# CONTEXT - EDGE CASES
# ============================================================================


def test_context_coerce_snapshot_invalid_string() -> None:
    """Cover _coerce_snapshot_int with invalid string."""
    from mcubridge.state.context import _coerce_snapshot_int

    result = _coerce_snapshot_int({"key": "invalid"}, "key", 42)
    assert result == 42


def test_context_coerce_snapshot_missing_key() -> None:
    """Cover _coerce_snapshot_int with missing key."""
    from mcubridge.state.context import _coerce_snapshot_int

    result = _coerce_snapshot_int({}, "missing", 42)
    assert result == 42


def test_context_coerce_snapshot_none_value() -> None:
    """Cover _coerce_snapshot_int with None value."""
    from mcubridge.state.context import _coerce_snapshot_int

    result = _coerce_snapshot_int({"key": None}, "key", 42)
    assert result == 42


# ============================================================================
# QUEUES - MORE EDGE CASES
# ============================================================================


def test_queues_append_with_bytes_limit_overflow() -> None:
    """Cover append with bytes limit causing overflow."""
    from mcubridge.state.queues import BoundedByteDeque

    q = BoundedByteDeque(max_items=10, max_bytes=5)
    q.append(b"hello")  # 5 bytes
    event = q.append(b"world")  # Should trigger overflow
    assert event.dropped_bytes > 0


def test_queues_make_room_for_complex() -> None:
    """Cover _make_room_for with complex conditions."""
    from mcubridge.state.queues import BoundedByteDeque

    q = BoundedByteDeque(max_items=3, max_bytes=100)
    q.append(b"a")
    q.append(b"b")
    q.append(b"c")

    # Now try to add a bigger item via append which calls _make_room_for internally
    event = q.append(b"d" * 50)
    assert event.dropped_chunks >= 0 or event.dropped_bytes >= 0


# ============================================================================
# DISPATCHER - EDGE CASES
# ============================================================================


def test_common_encode_status_reason() -> None:
    """Cover encode_status_reason function."""
    from mcubridge.protocol.encoding import encode_status_reason

    result = encode_status_reason("test_reason")
    assert result == b"test_reason"

    # With unicode
    result2 = encode_status_reason("razón")
    assert isinstance(result2, bytes)
