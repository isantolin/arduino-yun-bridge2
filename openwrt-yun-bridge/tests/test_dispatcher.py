"""Unit tests for BridgeDispatcher."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yunbridge.protocol.topics import Topic, TopicRoute
from yunbridge.rpc.protocol import Command, RESPONSE_OFFSET, Status
from yunbridge.services.dispatcher import BridgeDispatcher
from yunbridge.services.routers import MCUHandlerRegistry, MQTTRouter


def _make_dispatcher(
    *,
    is_link_synchronized: bool = True,
    is_topic_action_allowed: bool = True,
) -> BridgeDispatcher:
    registry = MCUHandlerRegistry()
    router = MQTTRouter()

    async def _send_frame(_cmd: int, _payload: bytes) -> bool:
        return True

    async def _ack(*_args, **_kwargs) -> None:
        return None

    async def _reject(*_args, **_kwargs) -> None:
        return None

    async def _publish(*_args, **_kwargs) -> None:
        return None

    return BridgeDispatcher(
        mcu_registry=registry,
        mqtt_router=router,
        send_frame=AsyncMock(side_effect=_send_frame),
        acknowledge_frame=AsyncMock(side_effect=_ack),
        is_link_synchronized=lambda: is_link_synchronized,
        is_topic_action_allowed=lambda _topic, _action: is_topic_action_allowed,
        reject_topic_action=AsyncMock(side_effect=_reject),
        publish_bridge_snapshot=AsyncMock(side_effect=_publish),
    )


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_rejects_pre_sync_non_status_sends_malformed() -> None:
    dispatcher = _make_dispatcher(is_link_synchronized=False)

    cmd = Command.CMD_CONSOLE_WRITE.value
    payload = b"abcdef" * 50

    await dispatcher.dispatch_mcu_frame(cmd, payload)

    dispatcher.acknowledge_frame.assert_awaited_once()
    args, kwargs = dispatcher.acknowledge_frame.call_args
    assert args[0] == cmd
    assert kwargs["status"] == Status.MALFORMED
    assert kwargs["extra"]


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_allows_status_frames_pre_sync() -> None:
    dispatcher = _make_dispatcher(is_link_synchronized=False)

    # Status frames are allowed pre-sync and do not get auto-acked.
    await dispatcher.dispatch_mcu_frame(Status.OK.value, b"hello")

    dispatcher.acknowledge_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_allows_link_sync_response_pre_sync() -> None:
    dispatcher = _make_dispatcher(is_link_synchronized=False)

    handled: list[bytes] = []

    async def _handler(payload: bytes):
        handled.append(payload)
        return True

    dispatcher.mcu_registry.register(Command.CMD_LINK_SYNC_RESP.value, _handler)

    await dispatcher.dispatch_mcu_frame(Command.CMD_LINK_SYNC_RESP.value, b"x")

    assert handled == [b"x"]


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_handler_false_skips_ack() -> None:
    dispatcher = _make_dispatcher()

    async def _handler(_payload: bytes):
        return False

    dispatcher.mcu_registry.register(Command.CMD_CONSOLE_WRITE.value, _handler)

    await dispatcher.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"hi")

    dispatcher.acknowledge_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_handler_success_acks() -> None:
    dispatcher = _make_dispatcher()

    async def _handler(_payload: bytes):
        return True

    dispatcher.mcu_registry.register(Command.CMD_CONSOLE_WRITE.value, _handler)

    await dispatcher.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"hi")

    dispatcher.acknowledge_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value)


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_handler_exception_sends_error_for_requests() -> None:
    dispatcher = _make_dispatcher()

    async def _handler(_payload: bytes):
        raise RuntimeError("boom")

    dispatcher.mcu_registry.register(Command.CMD_CONSOLE_WRITE.value, _handler)

    await dispatcher.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"hi")

    dispatcher.send_frame.assert_awaited_once_with(Status.ERROR.value, b"Internal Error")


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_unhandled_request_sends_not_implemented() -> None:
    dispatcher = _make_dispatcher()

    await dispatcher.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"")

    dispatcher.send_frame.assert_awaited_once_with(Status.NOT_IMPLEMENTED.value, b"")


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_orphaned_response_is_ignored() -> None:
    dispatcher = _make_dispatcher()

    await dispatcher.dispatch_mcu_frame(RESPONSE_OFFSET + 1, b"")

    dispatcher.send_frame.assert_not_awaited()
    dispatcher.acknowledge_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_mqtt_message_ignores_unmatched_prefix() -> None:
    dispatcher = _make_dispatcher()

    inbound = SimpleNamespace(topic="other/prefix", payload=b"x")

    def _parse(_topic: str):
        return None

    await dispatcher.dispatch_mqtt_message(inbound, _parse)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_dispatch_mqtt_message_ignores_missing_segments() -> None:
    dispatcher = _make_dispatcher()

    inbound = SimpleNamespace(topic="br/system", payload=b"x")

    def _parse(_topic: str):
        return TopicRoute(raw="br/system", prefix="br", topic=Topic.SYSTEM, segments=())

    await dispatcher.dispatch_mqtt_message(inbound, _parse)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_dispatch_mqtt_message_unhandled_logs_and_returns() -> None:
    dispatcher = _make_dispatcher()

    inbound = SimpleNamespace(topic="br/system/status", payload=b"x")

    def _parse(_topic: str):
        return TopicRoute(
            raw="br/system/status",
            prefix="br",
            topic=Topic.SYSTEM,
            segments=("status",),
        )

    await dispatcher.dispatch_mqtt_message(inbound, _parse)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_handle_bridge_topic_publishes_snapshots() -> None:
    dispatcher = _make_dispatcher()

    route = TopicRoute(
        raw="br/system/bridge/handshake/get",
        prefix="br",
        topic=Topic.SYSTEM,
        segments=("bridge", "handshake", "get"),
    )
    inbound = SimpleNamespace(topic=route.raw, payload=b"")

    ok = await dispatcher._handle_system_topic(route, inbound)  # type: ignore[arg-type]
    assert ok is True

    dispatcher.publish_bridge_snapshot.assert_awaited_once_with("handshake", inbound)


def test_payload_bytes_supports_common_types() -> None:
    assert BridgeDispatcher._payload_bytes(b"x") == b"x"
    assert BridgeDispatcher._payload_bytes(bytearray(b"x")) == b"x"
    assert BridgeDispatcher._payload_bytes(memoryview(b"x")) == b"x"
    assert BridgeDispatcher._payload_bytes(None) == b""
    assert BridgeDispatcher._payload_bytes("hi") == b"hi"
    assert BridgeDispatcher._payload_bytes(1) == b"1"
    assert BridgeDispatcher._payload_bytes(1.5) == b"1.5"

    with pytest.raises(TypeError):
        BridgeDispatcher._payload_bytes(object())


def test_pin_action_from_parts() -> None:
    assert BridgeDispatcher._pin_action_from_parts([]) is None
    assert BridgeDispatcher._pin_action_from_parts(["br", "digital"]) is None
    assert BridgeDispatcher._pin_action_from_parts(["br", "digital", "13"]) == "write"
    assert BridgeDispatcher._pin_action_from_parts(["br", "digital", "13", "read"]) == "read"
    assert BridgeDispatcher._pin_action_from_parts(["br", "digital", "13", ""]) is None
