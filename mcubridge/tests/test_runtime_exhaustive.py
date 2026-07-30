"""Exhaustive tests for mcubridge.services.runtime module. [SIL-2]"""

from __future__ import annotations
from mcubridge.protocol.topics import parse_topic

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb, protocol
from mcubridge.services.runtime import BridgeRequest, BridgeService
from mcubridge.state.context import RuntimeState


@pytest.fixture
def runtime_setup(
    tmp_path: Path,
) -> tuple[BridgeService, RuntimeState, AsyncMock]:
    tmp_dir = str(tmp_path)
    config = load_runtime_config(
        {
            "cloud_spool_dir": tmp_dir,
            "file_system_root": tmp_dir,
            "allow_non_tmp_paths": True,
        }
    )
    state = RuntimeState()
    transport = AsyncMock()

    service = BridgeService(config, state, transport)
    return service, state, transport


@pytest.mark.asyncio
async def test_on_serial_connected_and_disconnected(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _transport = runtime_setup
    handshake = AsyncMock()
    handshake.synchronize = AsyncMock(return_value=None)
    handshake.clear_handshake_expectations = MagicMock()
    service.handshake = handshake

    await service.on_serial_connected()
    handshake.synchronize.assert_called_once()

    await service.on_serial_disconnected()
    handshake.clear_handshake_expectations.assert_called_once()


@pytest.mark.asyncio
async def test_handle_mcu_frame_pre_sync_denied(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _transport = runtime_setup
    state.link_session_key = None

    res = await service.handle_mcu_frame(command_id=protocol.Command.CMD_SET_PIN_MODE.value, sequence_id=1, payload=b"")
    assert res is None or res is False


@pytest.mark.asyncio
async def test_handle_mcu_frame_handshake_routing(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _transport = runtime_setup
    mock_handler = AsyncMock(return_value=True)
    service.mcu_registry[protocol.Command.CMD_LINK_SYNC_RESP.value] = mock_handler

    # Handshake frame route directly to registered handler
    await service.handle_mcu_frame(command_id=protocol.Command.CMD_LINK_SYNC_RESP.value, sequence_id=1, payload=b"")
    mock_handler.assert_called_once()


@pytest.mark.asyncio
async def test_handle_mcu_frame_rpc_handlers(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _transport = runtime_setup
    state.link_session_key = b"0123456789abcdef0123456789abcdef"

    # 1. Analog Read
    req_analog = pb.PinControlData(pin=0)
    await service.handle_mcu_frame(
        command_id=protocol.Command.CMD_ANALOG_READ_RESP.value, sequence_id=10, payload=req_analog
    )

    # 2. Digital Read
    req_digital = pb.PinControlData(pin=13, state="1")
    await service.handle_mcu_frame(
        command_id=protocol.Command.CMD_DIGITAL_READ_RESP.value, sequence_id=11, payload=req_digital
    )

    # 3. Version Response
    req_ver = pb.VersionResponse(major=2, minor=8, patch=5)
    await service.handle_mcu_frame(
        command_id=protocol.Command.CMD_GET_VERSION_RESP.value, sequence_id=12, payload=req_ver
    )

    # 4. Free Memory Response
    req_mem = pb.FreeMemoryResponse(value=2048)
    await service.handle_mcu_frame(
        command_id=protocol.Command.CMD_GET_FREE_MEMORY_RESP.value, sequence_id=13, payload=req_mem
    )




@pytest.mark.asyncio
async def test_handle_request_routing(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, transport = runtime_setup
    state.link_session_key = b"0123456789abcdef0123456789abcdef"
    transport.send.return_value = True

    route = parse_topic("br", "br/d/13/mode")
    assert route is not None
    req = BridgeRequest(topic="br/d/13/mode", payload=b"1")

    handle_pin_fn = getattr(service, "_handle_pin")
    await handle_pin_fn(route, req)
    transport.send.assert_called_once()


@pytest.mark.asyncio
async def test_enqueue_cloud_spool_and_flush(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _transport = runtime_setup

    msg = pb.CloudQueuedPublish(
        topic_name="br/status",
        payload=b"online",
    )
    await service.enqueue_cloud(msg)
    spool = getattr(service, "_cloud_spool")
    assert spool is not None
    assert await spool.length() >= 1

    # Drain spool
    popped_bytes = await spool.popleft()
    assert popped_bytes is not None
    msg_popped = pb.CloudQueuedPublish.FromString(popped_bytes)
    assert msg_popped.topic_name == "br/status"
