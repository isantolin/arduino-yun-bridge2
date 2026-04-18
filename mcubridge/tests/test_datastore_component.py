"""Unit tests for mcubridge.services.datastore (SIL-2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import DatastoreAction
from mcubridge.protocol.topics import Topic
from mcubridge.services.base import BridgeContext
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.state.context import RuntimeState, create_runtime_state
from tests._helpers import make_mqtt_msg, make_route


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    import tempfile
    return RuntimeConfig(
        serial_port="/dev/null",
        mqtt_topic="br",
        file_system_root=tempfile.mkdtemp(prefix="mcubridge-test-fs-"),
        mqtt_spool_dir=tempfile.mkdtemp(prefix="mcubridge-test-spool-"),
        serial_shared_secret=b"s_e_c_r_e_t_mock",
    )


@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig) -> RuntimeState:
    state = create_runtime_state(runtime_config)
    return state


@pytest.fixture
def ctx(runtime_config: RuntimeConfig, runtime_state: RuntimeState) -> MagicMock:
    c = MagicMock(spec=BridgeContext)
    c.config = runtime_config
    c.state = runtime_state
    c.serial_flow = MagicMock()
    c.serial_flow.send = AsyncMock(return_value=True)
    c.serial_flow.acknowledge = AsyncMock()
    return c


@pytest.mark.asyncio
async def test_handle_put_success(
    ctx: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = DatastoreComponent(runtime_config, runtime_state, ctx)
    key = "testkey"
    value = b"testvalue"
    payload = structures.DatastorePutPacket(key=key, value=value).encode()

    with patch("mcubridge.state.context.RuntimeState.publish", new_callable=AsyncMock) as mock_pub:
        await component.handle_put(0, payload)

        assert runtime_state.datastore.get(key) == "testvalue"
        assert mock_pub.called


@pytest.mark.asyncio
async def test_handle_get_request_success(
    ctx: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = DatastoreComponent(runtime_config, runtime_state, ctx)
    runtime_state.datastore["mykey"] = "myvalue"
    payload = structures.DatastoreGetPacket(key="mykey").encode()

    with patch("mcubridge.state.context.RuntimeState.publish", new_callable=AsyncMock) as mock_pub:
        await component.handle_get_request(0, payload)

        ctx.serial_flow.send.assert_called_once()
        # Verify it published the result to MQTT as well
        assert mock_pub.called


@pytest.mark.asyncio
async def test_handle_mqtt_put(
    ctx: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = DatastoreComponent(runtime_config, runtime_state, ctx)

    with patch("mcubridge.state.context.RuntimeState.publish", new_callable=AsyncMock) as mock_pub:
        await component.handle_mqtt(
            make_route(Topic.DATASTORE, DatastoreAction.PUT.value, "newkey"),
            make_mqtt_msg(b"newval"),
        )

        assert runtime_state.datastore.get("newkey") == "newval"
        assert mock_pub.called


@pytest.mark.asyncio
async def test_handle_mqtt_get_request(
    ctx: MagicMock,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> None:
    component = DatastoreComponent(runtime_config, runtime_state, ctx)
    runtime_state.datastore["reqkey"] = "reqval"

    with patch("mcubridge.state.context.RuntimeState.publish", new_callable=AsyncMock) as mock_pub:
        # Simulate a get request via MQTT
        await component.handle_mqtt(
            make_route(Topic.DATASTORE, DatastoreAction.GET.value, "reqkey", "request"),
            make_mqtt_msg(b""),
        )

        mock_pub.assert_called()
        args, kwargs = mock_pub.call_args
        pld = kwargs.get("payload") or args[1]
        assert pld == b"reqval"
