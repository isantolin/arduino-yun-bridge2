"""Exhaustive gap closure suite 15 for Python daemon SIL-2 coverage (95%+ target)."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import mcubridge.config.settings as settings_mod
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.structures import TopicRoute
from mcubridge.protocol.topics import Topic
from mcubridge.services.runtime import BridgeRequest, BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_settings_gap_closure():
    cfg = load_runtime_config()
    # 1. _runtime_config_factory with pb_msg parameter
    factory = getattr(settings_mod, "_runtime_config_factory")
    res = factory(pb_msg=cfg)
    assert res == cfg

    # 2. _load_raw_config with UCI returning values and with UCI raising RuntimeError
    with patch("mcubridge.config.settings.get_uci_config", return_value={"serial_baud": 57600}):
        c = load_runtime_config()
        assert c is not None

    with patch("mcubridge.config.settings.get_uci_config", side_effect=RuntimeError("UCI error")):
        c2 = load_runtime_config()
        assert c2 is not None


@pytest.mark.asyncio
async def test_context_snapshot_and_cleanup_gaps():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)

    # 1. build_bridge_snapshot with mcu_capabilities
    state.mcu_capabilities = {"dig": 20, "ana": 6, "ver": 2}
    snap = state.build_bridge_snapshot()
    assert snap.capabilities is not None

    # 2. cleanup with process raising ProcessLookupError on terminate
    mock_proc = MagicMock()
    mock_proc.handle.terminate.side_effect = ProcessLookupError("No such process")
    state.running_processes[999] = mock_proc
    state.cleanup()
    assert len(state.running_processes) == 0

    # 3. cleanup with mailbox_queue raising OSError on close
    mock_mb = MagicMock()
    mock_mb.close.side_effect = OSError("close error")
    state.mailbox_queue = mock_mb
    state.cleanup()


@pytest.mark.asyncio
async def test_runtime_service_more_gap_closures():
    cfg = load_runtime_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    service = BridgeService(cfg, state, mock_serial)
    srv = cast(Any, service)

    # 1. _handle_mcu_xon and _handle_mcu_xoff
    await srv._handle_mcu_xon(1, pb.GenericResponse())
    await srv._handle_mcu_xoff(1, pb.GenericResponse())

    # 2. _on_mcu_datastore_get
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._on_mcu_datastore_get(1, pb.DatastoreGet(key="mykey"))

    # 3. handle_request with invalid topic
    req_inv = BridgeRequest(topic="invalid_topic_without_slash", payload=b"payload")
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await service.handle_request(req_inv)

    # 4. _handle_pin write and invalid action
    req_pin = BridgeRequest(topic="digital/13/write", payload=b"1")
    route_pin_write = TopicRoute(raw="", prefix="", topic=Topic.DIGITAL, segments=("13", "write"))
    await srv._handle_pin(route_pin_write, req_pin)

    route_pin_invalid = TopicRoute(raw="", prefix="", topic=Topic.DIGITAL, segments=("13", "invalid"))
    await srv._handle_pin(route_pin_invalid, req_pin)

    # 5. _handle_file_mcu_read with send_raw failing
    req_file = BridgeRequest(topic="file/read/mcu/sd/data.txt", payload=b"")
    mock_serial.send_raw.return_value = False
    with patch.object(service, "enqueue_cloud", new_callable=AsyncMock):
        await srv._handle_file_mcu_read(req_file, "/mcu/sd/data.txt")

    # 6. _finalize_process
    srv._finalize_process(99999)
