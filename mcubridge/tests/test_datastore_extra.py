"""Extra coverage for mcubridge.services.datastore."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Topic
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.state.context import create_runtime_state

from tests._helpers import make_mqtt_msg, make_route


@pytest.mark.asyncio
async def test_datastore_handle_put_malformed() -> None:
    config = RuntimeConfig(serial_shared_secret=b"1234")
    state = create_runtime_state(config)
    ctx = MagicMock()
    ctx.send_frame = AsyncMock()

    component = DatastoreComponent(config, state, ctx)

    # Invalid topic action
    route = make_route(Topic.DATASTORE, "invalid", "key")
    msg = make_mqtt_msg(b"val")

    res = await component.handle_mqtt(route, msg)
    assert res is False
