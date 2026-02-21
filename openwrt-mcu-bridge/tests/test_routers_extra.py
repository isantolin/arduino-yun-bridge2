"""Extra coverage for mcubridge.router.routers."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.router.routers import MQTTRouter

@pytest.mark.asyncio
async def test_mqtt_router_dispatch_multi_handlers() -> None:
    router = MQTTRouter()
    h1 = AsyncMock(return_value=False)
    h2 = AsyncMock(return_value=True)
    router.register(Topic.DIGITAL, h1)
    router.register(Topic.DIGITAL, h2)

    route = TopicRoute(raw="r", prefix="p", topic=Topic.DIGITAL, segments=())
    assert await router.dispatch(route, MagicMock()) is True
    assert h1.call_count == 1
    assert h2.call_count == 1

@pytest.mark.asyncio
async def test_mqtt_router_dispatch_no_match() -> None:
    router = MQTTRouter()
    route = TopicRoute(raw="r", prefix="p", topic=Topic.DIGITAL, segments=())
    assert await router.dispatch(route, MagicMock()) is False
