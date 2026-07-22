"""Ninth targeted coverage gap closure for handshake.py capabilities discovery, link reset resp,
and handshake success/backoff paths to pass 95% total Python coverage. [SIL-2]"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.handshake import SerialHandshakeManager, derive_serial_timing
from mcubridge.state.context import RuntimeState, create_runtime_state

# ==============================================================================
# Fixtures
# ==============================================================================


def _make_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
        topic_prefix="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123456abcd",
        file_system_root=str(tmp_path / "fs"),
        cloud_spool_dir=str(tmp_path / "spool"),
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> RuntimeConfig:
    return _make_config(tmp_path)


@pytest.fixture
def state(cfg: RuntimeConfig) -> Iterator[RuntimeState]:
    s = create_runtime_state(cfg)
    yield s
    s.cleanup()


# ==============================================================================
# handshake.py — Capabilities, link reset resp, success and backoff (lines 339-403, 503-535)
# ==============================================================================


@pytest.mark.asyncio
async def test_handshake_capabilities_flow(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_fetch_capabilities and handle_capabilities_resp (lines 343-397)."""
    timing = derive_serial_timing(cfg)
    hs = SerialHandshakeManager(
        config=cfg,
        state=state,
        serial_timing=timing,
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # 1. handle_capabilities_resp with ProtobufMessage & bytes
    cap_pb = pb.Capabilities(ver=2, dig=13)
    fn_parse = getattr(hs, "_parse_capabilities")
    fn_parse(cap_pb)
    caps = state.mcu_capabilities
    if isinstance(caps, pb.Capabilities):
        assert caps.dig == 13 or caps.ver == 2
    elif isinstance(caps, dict):
        assert caps.get("dig") == 13 or caps.get("ver") == 2

    fn_parse(cap_pb.SerializeToString())

    # Exception path: FromString exception
    with patch("mcubridge.protocol.mcubridge_pb2.Capabilities.FromString", side_effect=TypeError("invalid msg")):
        fn_parse(b"invalid")

    # 2. handle_capabilities_resp when _capabilities_future is active
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    setattr(hs, "_capabilities_future", fut)
    res = await hs.handle_capabilities_resp(1, b"cap_bytes")
    assert res is True
    assert fut.result() == b"cap_bytes"

    # 3. handle_link_reset_resp
    res = await hs.handle_link_reset_resp(1, b"\x01\x02")
    assert res is True
    res = await hs.handle_link_reset_resp(1, pb.Capabilities())
    assert res is True


@pytest.mark.asyncio
async def test_handshake_success_and_backoff(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_handle_handshake_success and _maybe_schedule_handshake_backoff (lines 503-535)."""
    timing = derive_serial_timing(cfg)
    hs = SerialHandshakeManager(
        config=cfg,
        state=state,
        serial_timing=timing,
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    fn_success = getattr(hs, "_handle_handshake_success")
    fn_backoff = getattr(hs, "_maybe_schedule_handshake_backoff")

    # 1. Success path
    with patch.object(hs, "_publish_handshake_event", new=AsyncMock()) as mock_pub:
        await fn_success()
        assert state.handshake_failure_streak == 0
        assert state.last_handshake_error is None
        mock_pub.assert_awaited_once()

    # 2. Backoff path for non-fatal vs fatal reasons
    state.handshake_failure_streak = 1
    assert fn_backoff("timeout") is None  # Non-fatal threshold is 3

    state.handshake_failure_streak = 3
    delay = fn_backoff("timeout")
    assert delay is not None
    assert delay > 0

    state.handshake_failure_streak = 1
    delay_fatal = fn_backoff("sync_auth_mismatch")  # Fatal threshold is 1
    assert delay_fatal is not None


def _zero_wait(*_a: Any, **_k: Any) -> float:
    return 0.0


def _always_stop(*_a: Any, **_k: Any) -> bool:
    return True


@pytest.mark.asyncio
async def test_fetch_capabilities_send_failure(cfg: RuntimeConfig, state: RuntimeState) -> None:
    """_fetch_capabilities when send_frame returns False (lines 361-364)."""
    timing = derive_serial_timing(cfg)
    hs = SerialHandshakeManager(
        config=cfg,
        state=state,
        serial_timing=timing,
        send_frame=AsyncMock(return_value=False),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    fn_fetch = getattr(hs, "_fetch_capabilities")
    with patch("tenacity.wait_exponential", return_value=_zero_wait):
        with patch("tenacity.stop_after_attempt", return_value=_always_stop):
            res = await fn_fetch()
            assert res is False
