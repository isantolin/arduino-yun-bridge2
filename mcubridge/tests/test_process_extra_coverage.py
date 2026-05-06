import pytest
from unittest.mock import AsyncMock, MagicMock
from typing import Any
from mcubridge.services.process import ProcessComponent
from mcubridge.protocol.structures import TopicRoute
from mcubridge.services.file import FileAction


@pytest.fixture
def mock_serial_flow() -> Any:
    return AsyncMock()


@pytest.fixture
def mock_mqtt_flow() -> Any:
    return AsyncMock()


@pytest.fixture
def mock_state() -> Any:
    state = MagicMock()
    state.mqtt_topic_prefix = "br"
    return state


@pytest.mark.asyncio
async def test_process_component_unsupported_actions(
    mock_serial_flow: Any, mock_mqtt_flow: Any, mock_state: Any
) -> None:
    proc_comp = ProcessComponent(
        MagicMock(), mock_state, mock_serial_flow, mock_mqtt_flow
    )

    route = MagicMock(spec=TopicRoute)
    route.action = FileAction.READ  # Unsupported for process
    route.remainder = ["cmd"]
    inbound = MagicMock(payload=b"")

    res = await proc_comp.handle_mqtt(route, inbound)
    assert res


@pytest.mark.asyncio
async def test_process_component_handle_mqtt_poll_error(
    mock_serial_flow: Any, mock_mqtt_flow: Any, mock_state: Any
) -> None:
    proc_comp = ProcessComponent(
        MagicMock(), mock_state, mock_serial_flow, mock_mqtt_flow
    )
    route = MagicMock(spec=TopicRoute)
    route.action = None
    route.remainder = ["poll", "1"]
    inbound = MagicMock(payload=b"")

    await proc_comp.handle_mqtt(route, inbound)


@pytest.mark.asyncio
async def test_process_component_kill_error(
    mock_serial_flow: Any, mock_mqtt_flow: Any, mock_state: Any
) -> None:
    proc_comp = ProcessComponent(
        MagicMock(), mock_state, mock_serial_flow, mock_mqtt_flow
    )
    route = MagicMock(spec=TopicRoute)
    route.action = None
    route.remainder = ["kill", "1"]
    inbound = MagicMock(payload=b"")

    await proc_comp.handle_mqtt(route, inbound)
