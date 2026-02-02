"""Tests for the DatastoreComponent."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import Command
from mcubridge.services.components.base import BridgeContext
from mcubridge.services.components.datastore import DatastoreComponent
from mcubridge.state.context import create_runtime_state


@pytest_asyncio.fixture
async def datastore_component() -> DatastoreComponent:
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )
    state = create_runtime_state(config)
    ctx = AsyncMock(spec=BridgeContext)

    # Mock schedule_background to just await the coroutine immediately for testing
    async def _schedule(coro):
        await coro

    ctx.schedule_background.side_effect = _schedule

    component = DatastoreComponent(config, state, ctx)
    return component


@pytest.mark.asyncio
async def test_handle_put_success(datastore_component: DatastoreComponent) -> None:
    key = b"key1"
    value = b"value1"
    # Payload: key_len (1 byte) + key + value_len (1 byte) + value
    payload = (
        struct.pack(protocol.UINT8_FORMAT, len(key)) + key + struct.pack(protocol.UINT8_FORMAT, len(value)) + value
    )

    # Mock _publish_value
    with patch.object(datastore_component, "_publish_value", new_callable=AsyncMock) as mock_pub:
        result = await datastore_component.handle_put(payload)

        assert result is True
        assert datastore_component.state.datastore["key1"] == "value1"
        mock_pub.assert_awaited_once_with("key1", value)


@pytest.mark.asyncio
async def test_handle_put_malformed(datastore_component: DatastoreComponent) -> None:
    # Too short
    assert await datastore_component.handle_put(b"") is False

    # Missing value length
    key = b"k"
    payload = struct.pack(protocol.UINT8_FORMAT, len(key)) + key
    assert await datastore_component.handle_put(payload) is False


@pytest.mark.asyncio
async def test_handle_get_request_success(
    datastore_component: DatastoreComponent,
) -> None:
    # Pre-populate datastore
    datastore_component.state.datastore["key1"] = "value1"

    key = b"key1"
    payload = struct.pack(protocol.UINT8_FORMAT, len(key)) + key

    await datastore_component.handle_get_request(payload)

    datastore_component.ctx.send_frame.assert_awaited_once()
    args = datastore_component.ctx.send_frame.call_args[0]
    assert args[0] == Command.CMD_DATASTORE_GET_RESP.value

    # Response: value_len (1 byte) + value
    resp = args[1]
    assert resp[0] == len("value1")
    assert resp[1:] == b"value1"


@pytest.mark.asyncio
async def test_handle_get_request_missing(
    datastore_component: DatastoreComponent,
) -> None:
    key = b"missing"
    payload = struct.pack(protocol.UINT8_FORMAT, len(key)) + key

    await datastore_component.handle_get_request(payload)

    datastore_component.ctx.send_frame.assert_awaited_once()
    args = datastore_component.ctx.send_frame.call_args[0]
    assert args[0] == Command.CMD_DATASTORE_GET_RESP.value

    # Should return empty string
    resp = args[1]
    assert resp[0] == 0
    assert len(resp) == 1
