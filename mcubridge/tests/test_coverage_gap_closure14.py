"""Exhaustive gap closure suite 14 for Python daemon SIL-2 coverage (95%+ target)."""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_handshake_successful_link_sync_path():
    cfg = load_runtime_config()
    cfg.serial_shared_secret = b"12345678901234567890123456789012"
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)
    hs = service.handshake

    nonce = b"\x01" * protocol.AEAD_NONCE_SIZE
    state.link_handshake_nonce = nonce
    tag = hs.calculate_handshake_tag(cfg.serial_shared_secret, nonce)
    state.link_expected_tag = tag

    pkt = pb.LinkSync(nonce=nonce, tag=tag)
    with (
        patch.object(hs, "_handle_handshake_success", new_callable=AsyncMock),
        patch.object(hs, "_fetch_capabilities_with_delay", new_callable=AsyncMock),
    ):
        res = await hs.handle_link_sync_resp(1, pkt)
        assert res is True
        assert state.link_session_key is not None


@pytest.mark.asyncio
async def test_context_reconfigure_and_spool_paths():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)

    # 1. mark_synchronized with link_sync_event set
    state.link_sync_event = asyncio.Event()
    state.mark_synchronized()
    assert state.link_sync_event.is_set()

    state.mark_transport_disconnected()
    assert not state.link_sync_event.is_set()

    # 2. configure with allow_non_tmp_paths and custom file_system_root
    state.allow_non_tmp_paths = True
    state.file_system_root = "/tmp/test_context_root"
    state.configure()
    assert state.datastore_cache is not None

    # 3. safe_close resource raising OSError
    mock_bad_res = MagicMock()
    mock_bad_res.close.side_effect = OSError("close error")
    state.datastore_cache = mock_bad_res
    state.configure()


@pytest.mark.asyncio
async def test_client_sdk_gap_closure():
    from mcubridge_client import Bridge
    from mcubridge_client.env import read_uci_general

    # 1. connect when channel is already set
    client = Bridge(socket_path="/tmp/nonexistent.sock")
    client.channel = MagicMock()
    with patch("mcubridge_client.Channel"), patch.object(client, "_console_listener", new_callable=AsyncMock):
        await client.connect()

    # 2. disconnect with real task
    async def dummy_coro():
        await asyncio.sleep(0.01)

    t = asyncio.create_task(dummy_coro())
    client._listener_task = t  # type: ignore[reportPrivateUsage]
    await client.disconnect()

    # 3. console_listener when stub is None
    client.stub = None
    await client._console_listener()  # type: ignore[reportPrivateUsage]

    # 4. read_uci_general RuntimeError handling
    with (
        patch("mcubridge_client.env._is_openwrt", return_value=True),
        patch("mcubridge.config.common.get_uci_config", side_effect=RuntimeError("UCI error")),
    ):
        res = read_uci_general()
        assert res == {}


@pytest.mark.asyncio
async def test_runtime_service_remaining_gaps():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # 1. _on_pin_resp for ANALOG
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._on_mcu_analog_read_resp(1, pb.AnalogReadResponse(value=512))

    # 2. _handle_mcu_status with status error and str payload
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_mcu_status(protocol.Status.ERROR, 1, "Status error message")
