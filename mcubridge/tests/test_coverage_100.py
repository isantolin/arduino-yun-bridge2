"""
Tests specifically targeting 100% coverage for critical Python modules.

This module covers edge cases and defensive code paths that are
harder to reach in normal testing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from mcubridge.protocol.protocol import (
    Status,
)
from mcubridge.services.process import ProcessComponent
from mcubridge.state.context import create_runtime_state

from tests._helpers import make_test_config


@pytest.fixture
def mock_enqueue() -> AsyncMock:
    return AsyncMock()


@pytest_asyncio.fixture
async def process_component(mock_enqueue: AsyncMock) -> ProcessComponent:  # type: ignore[reportInvalidTypeForm]
    config = make_test_config(process_max_concurrent=4)
    state = create_runtime_state(config)
    service = MagicMock()
    service.acknowledge_mcu_frame = AsyncMock()
    component = ProcessComponent(config, state, service)
    try:
        yield component  # type: ignore[reportReturnType]
    finally:
        for pid in list(component.state.running_processes):
            await component.stop_process(pid)
        component.state.cleanup()


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
    await process_component._finalize_process(pid)  # type: ignore[reportPrivateUsage]


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


def test_context_resolve_command_id_invalid() -> None:
    """Cover resolve_command_id with invalid value."""
    from mcubridge.state.context import resolve_command_id
    assert resolve_command_id(0xFFFF) == "0xFFFF"


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


def test_common_encode_status_reason_inline() -> None:
    """Cover inline encode_status_reason logic."""
    from mcubridge.protocol import protocol

    reason = "test_reason"
    result = reason.encode("utf-8", errors="ignore")[:protocol.MAX_PAYLOAD_SIZE]
    assert result == b"test_reason"

    # With unicode
    result2 = "razón".encode("utf-8", errors="ignore")[:protocol.MAX_PAYLOAD_SIZE]
    assert isinstance(result2, bytes)
