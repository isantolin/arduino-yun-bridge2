from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pytest

from mcubridge.protocol.topics import TopicRoute, parse_topic
from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import Command, Status, Topic
from mcubridge.services.dispatcher import BridgeDispatcher
from mcubridge.services.routers import MCUHandlerRegistry, MQTTRouter

from .mqtt_helpers import make_inbound_message


@dataclass(frozen=True)
class _Calls:
    items: list[tuple[str, Any]]

    def add(self, name: str, *args: Any) -> None:
        self.items.append((name, args))


class _FileComponent:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls

    async def handle_write(self, payload: bytes) -> bool:
        self._calls.add("file.handle_write", payload)
        return True

    async def handle_read(self, payload: bytes) -> bool:
        self._calls.add("file.handle_read", payload)
        return True

    async def handle_remove(self, payload: bytes) -> bool:
        self._calls.add("file.handle_remove", payload)
        return True

    async def handle_mqtt(
        self,
        identifier: str,
        remainder: list[str],
        payload: bytes,
        inbound: Any,
    ) -> None:
        self._calls.add("file.handle_mqtt", identifier, tuple(remainder), payload, inbound)


class _ConsoleComponent:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls

    async def handle_mqtt_input(self, payload: bytes, inbound: Any) -> bool:
        self._calls.add("console.handle_mqtt_input", payload, inbound)
        return True

    async def handle_xoff(self, payload: bytes) -> bool:
        self._calls.add("console.handle_xoff", payload)
        return True

    async def handle_xon(self, payload: bytes) -> bool:
        self._calls.add("console.handle_xon", payload)
        return True

    async def handle_write(self, payload: bytes) -> bool:
        self._calls.add("console.handle_write", payload)
        return True


class _DatastoreComponent:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls

    async def handle_put(self, payload: bytes) -> bool:
        self._calls.add("datastore.handle_put", payload)
        return True

    async def handle_get_request(self, payload: bytes) -> bool:
        self._calls.add("datastore.handle_get_request", payload)
        return True

    async def handle_mqtt(
        self,
        identifier: str,
        remainder: list[str],
        payload: bytes,
        payload_str: str,
        inbound: Any,
    ) -> None:
        self._calls.add(
            "datastore.handle_mqtt",
            identifier,
            tuple(remainder),
            payload,
            payload_str,
            inbound,
        )


class _MailboxComponent:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls

    async def handle_push(self, payload: bytes) -> bool:
        self._calls.add("mailbox.handle_push", payload)
        return True

    async def handle_available(self, payload: bytes) -> bool:
        self._calls.add("mailbox.handle_available", payload)
        return True

    async def handle_read(self, payload: bytes) -> bool:
        self._calls.add("mailbox.handle_read", payload)
        return True

    async def handle_processed(self, payload: bytes) -> bool:
        self._calls.add("mailbox.handle_processed", payload)
        return True

    async def handle_mqtt(self, identifier: str, payload: bytes, inbound: Any) -> None:
        self._calls.add("mailbox.handle_mqtt", identifier, payload, inbound)


class _PinComponent:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls

    async def handle_digital_read_resp(self, payload: bytes) -> bool:
        self._calls.add("pin.handle_digital_read_resp", payload)
        return True

    async def handle_analog_read_resp(self, payload: bytes) -> bool:
        self._calls.add("pin.handle_analog_read_resp", payload)
        return True

    async def handle_unexpected_mcu_request(self, command: Command, payload: bytes) -> bool:
        self._calls.add("pin.handle_unexpected_mcu_request", command, payload)
        return True

    async def handle_mqtt(
        self,
        topic: Topic,
        parts: list[str],
        payload_str: str,
        inbound: Any,
    ) -> None:
        self._calls.add("pin.handle_mqtt", topic, tuple(parts), payload_str, inbound)


class _ProcessComponent:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls

    async def handle_run(self, payload: bytes) -> bool:
        self._calls.add("process.handle_run", payload)
        return True

    async def handle_run_async(self, payload: bytes) -> bool:
        self._calls.add("process.handle_run_async", payload)
        return True

    async def handle_poll(self, payload: bytes) -> bool:
        self._calls.add("process.handle_poll", payload)
        return True


class _ShellComponent:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls

    async def handle_mqtt(self, parts: list[str], payload: bytes, inbound: Any) -> None:
        self._calls.add("shell.handle_mqtt", tuple(parts), payload, inbound)


class _SystemComponent:
    def __init__(self, calls: _Calls) -> None:
        self._calls = calls

    async def handle_get_free_memory_resp(self, payload: bytes) -> bool:
        self._calls.add("system.handle_get_free_memory_resp", payload)
        return True

    async def handle_get_version_resp(self, payload: bytes) -> bool:
        self._calls.add("system.handle_get_version_resp", payload)
        return True

    async def handle_set_baudrate_resp(self, payload: bytes) -> bool:
        self._calls.add("system.handle_set_baudrate_resp", payload)
        return True

    async def handle_mqtt(self, identifier: str, remainder: list[str], inbound: Any) -> bool:
        self._calls.add("system.handle_mqtt", identifier, tuple(remainder), inbound)
        return identifier != "nope"


def _make_dispatcher(
    calls: _Calls,
    *,
    is_link_synchronized: Callable[[], bool] | None = None,
    is_topic_action_allowed: Callable[[Topic | str, str], bool] | None = None,
) -> BridgeDispatcher:
    registry = MCUHandlerRegistry()
    router = MQTTRouter()

    async def _send_frame(command_id: int, payload: bytes) -> bool:
        calls.add("send_frame", command_id, payload)
        return True

    async def _acknowledge_frame(*args: Any, **kwargs: Any) -> None:
        calls.add("acknowledge_frame", args, kwargs)

    def _is_link_synchronized() -> bool:
        return True if is_link_synchronized is None else is_link_synchronized()

    def _is_topic_action_allowed(topic: Topic | str, action: str) -> bool:
        if is_topic_action_allowed is not None:
            return is_topic_action_allowed(topic, action)
        return True

    async def _reject_topic_action(inbound: Any, topic: Topic | str, action: str) -> None:
        calls.add("reject_topic_action", inbound, topic, action)

    async def _publish_bridge_snapshot(kind: str, inbound: Any) -> None:
        calls.add("publish_bridge_snapshot", kind, inbound)

    dispatcher = BridgeDispatcher(
        registry,
        router,
        _send_frame,
        _acknowledge_frame,
        _is_link_synchronized,
        _is_topic_action_allowed,
        _reject_topic_action,
        _publish_bridge_snapshot,
    )
    dispatcher.register_components(
        console=_ConsoleComponent(calls),
        datastore=_DatastoreComponent(calls),
        file=_FileComponent(calls),
        mailbox=_MailboxComponent(calls),
        pin=_PinComponent(calls),
        process=_ProcessComponent(calls),
        shell=_ShellComponent(calls),
        system=_SystemComponent(calls),
    )

    async def _handle_link_sync_resp(payload: bytes) -> bool:
        calls.add("handle_link_sync_resp", payload)
        return True

    async def _handle_link_reset_resp(payload: bytes) -> bool:
        calls.add("handle_link_reset_resp", payload)
        return True

    async def _handle_get_capabilities_resp(payload: bytes) -> bool:
        calls.add("handle_get_capabilities_resp", payload)
        return True

    async def _handle_ack(payload: bytes) -> None:
        calls.add("handle_ack", payload)

    def _status_handler_factory(status: Status):
        async def _handler(payload: bytes) -> None:
            calls.add("status_handler", status, payload)

        return _handler

    async def _handle_process_kill(payload: bytes) -> bool | None:
        calls.add("handle_process_kill", payload)
        return True

    dispatcher.register_system_handlers(
        handle_link_sync_resp=_handle_link_sync_resp,
        handle_link_reset_resp=_handle_link_reset_resp,
        handle_get_capabilities_resp=_handle_get_capabilities_resp,
        handle_ack=_handle_ack,
        status_handler_factory=_status_handler_factory,
        handle_process_kill=_handle_process_kill,
    )
    return dispatcher


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_rejects_pre_sync_without_reply_frames() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls, is_link_synchronized=lambda: False)

    await dispatcher.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"xyz")

    assert not any(name == "acknowledge_frame" for name, _ in calls.items)
    assert not any(name == "send_frame" for name, _ in calls.items)


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_allows_status_frames_pre_sync() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls, is_link_synchronized=lambda: False)

    await dispatcher.dispatch_mcu_frame(Status.ACK.value, b"")

    assert not any(name == "acknowledge_frame" for name, _ in calls.items)
    assert any(name == "handle_ack" for name, _ in calls.items)


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_handler_success_auto_acks() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)

    async def handler(payload: bytes) -> bool:
        calls.add("handler", payload)
        return True

    dispatcher.mcu_registry.register(Command.CMD_CONSOLE_WRITE.value, handler)
    await dispatcher.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"hello")

    assert ("handler", (b"hello",)) in calls.items
    assert any(name == "acknowledge_frame" for name, _ in calls.items)


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_handler_returns_false_no_ack() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)

    async def handler(_payload: bytes) -> bool:
        return False

    dispatcher.mcu_registry.register(Command.CMD_CONSOLE_WRITE.value, handler)
    await dispatcher.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"hello")

    assert not any(name == "acknowledge_frame" for name, _ in calls.items)


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_handler_exception_sends_error_for_request() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)

    async def handler(_payload: bytes) -> bool:
        raise RuntimeError("boom")

    dispatcher.mcu_registry.register(Command.CMD_CONSOLE_WRITE.value, handler)
    await dispatcher.dispatch_mcu_frame(Command.CMD_CONSOLE_WRITE.value, b"hello")

    assert any(name == "send_frame" and args[0] == Status.ERROR.value for name, args in calls.items)


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_unhandled_request_sends_not_implemented() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)

    await dispatcher.dispatch_mcu_frame(Command.CMD_LINK_SYNC.value, b"")

    assert any(name == "send_frame" and args[0] == Status.NOT_IMPLEMENTED.value for name, args in calls.items)


@pytest.mark.asyncio
async def test_dispatch_mcu_frame_orphaned_response_is_ignored() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)

    await dispatcher.dispatch_mcu_frame(Command.CMD_PROCESS_RUN_RESP.value, b"1")

    assert not any(name == "acknowledge_frame" for name, _ in calls.items)
    assert not any(name == "send_frame" for name, _ in calls.items)


def test_resolve_command_id_handles_command_status_unknown() -> None:
    """Verify resolve_command_id resolves Command, Status, and unknown IDs."""
    from mcubridge.state.context import resolve_command_id

    assert resolve_command_id(Command.CMD_CONSOLE_WRITE.value) == "CMD_CONSOLE_WRITE"
    assert resolve_command_id(Status.ACK.value) == "ACK"
    assert resolve_command_id(0xEE) == "0xEE"


@pytest.mark.asyncio
async def test_dispatch_mqtt_message_ignored_for_bad_prefix_or_missing_segments() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)
    inbound = make_inbound_message("other/prefix/console/in", payload=b"hi")
    await dispatcher.dispatch_mqtt_message(
        inbound,
        parse_topic_func=lambda name: parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, name),
    )
    assert calls.items == []

    inbound2 = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/console",
        payload=b"hi",
    )
    await dispatcher.dispatch_mqtt_message(
        inbound2,
        parse_topic_func=lambda name: parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, name),
    )
    assert calls.items == []


@pytest.mark.asyncio
async def test_dispatch_mqtt_message_router_error_is_caught() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)

    async def exploding(_route: TopicRoute, _msg: Any) -> bool:
        raise RuntimeError("boom")

    dispatcher.mqtt_router.register(Topic.CONSOLE, exploding)
    dispatcher.mqtt_router._handlers[Topic.CONSOLE] = [exploding]  # type: ignore[attr-defined]
    inbound = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/console/in",
        payload=b"hi",
    )
    await dispatcher.dispatch_mqtt_message(
        inbound,
        parse_topic_func=lambda name: parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, name),
    )
    assert calls.items == []


@pytest.mark.asyncio
async def test_console_topic_rejects_by_policy_and_accepts_payload_types() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(
        calls,
        is_topic_action_allowed=lambda _topic, _action: False,
    )
    inbound = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/console/in",
        payload=b"hello",
    )
    route = parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, str(inbound.topic))
    assert route is not None
    handled = await dispatcher._handle_console_topic(route, inbound)
    assert handled is True
    assert any(name == "reject_topic_action" for name, _ in calls.items)

    calls2 = _Calls([])
    dispatcher2 = _make_dispatcher(calls2)
    inbound2 = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/console/in",
        payload=b"hello",
    )
    route2 = parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, str(inbound2.topic))
    assert route2 is not None
    handled2 = await dispatcher2._handle_console_topic(route2, inbound2)
    assert handled2 is True
    assert any(name == "console.handle_mqtt_input" for name, _ in calls2.items)


@pytest.mark.asyncio
async def test_file_topic_requires_two_segments_and_calls_component() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)

    route1 = TopicRoute(
        raw=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/file",
        prefix=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        topic=Topic.FILE,
        segments=("read",),
    )
    inbound1 = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/file/read",
        payload=b"x",
    )
    assert await dispatcher._handle_file_topic(route1, inbound1) is False

    inbound2 = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/file/read/path",
        payload=bytearray(b"abc"),
    )
    route2 = parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, str(inbound2.topic))
    assert route2 is not None
    assert await dispatcher._handle_file_topic(route2, inbound2) is True
    assert any(name == "file.handle_mqtt" for name, _ in calls.items)


@pytest.mark.asyncio
async def test_datastore_topic_rejects_missing_identifier_and_calls_component() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)

    route1 = TopicRoute(
        raw=f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/datastore",
        prefix=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        topic=Topic.DATASTORE,
        segments=(),
    )
    inbound1 = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/datastore",
        payload=b"x",
    )
    assert await dispatcher._handle_datastore_topic(route1, inbound1) is False

    inbound2 = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/datastore/put/key",
        payload=memoryview(b"hi"),
    )
    route2 = parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, str(inbound2.topic))
    assert route2 is not None
    assert await dispatcher._handle_datastore_topic(route2, inbound2) is True
    assert any(name == "datastore.handle_mqtt" for name, _ in calls.items)


@pytest.mark.asyncio
async def test_pin_topic_action_deduction_and_policy() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(
        calls,
        is_topic_action_allowed=lambda _topic, action: action != "write",
    )
    inbound = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/d/13",
        payload=b"1",
    )
    route = parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, str(inbound.topic))
    assert route is not None
    assert await dispatcher._handle_pin_topic(route, inbound) is True
    assert any(name == "reject_topic_action" for name, _ in calls.items)

    calls2 = _Calls([])
    dispatcher2 = _make_dispatcher(calls2)
    inbound2 = make_inbound_message(
        f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/d/13/read",
        payload=b"",
    )
    route2 = parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, str(inbound2.topic))
    assert route2 is not None
    assert await dispatcher2._handle_pin_topic(route2, inbound2) is True
    assert any(name == "pin.handle_mqtt" for name, _ in calls2.items)


@pytest.mark.asyncio
async def test_system_topic_bridge_get_handlers_and_fallback_to_component() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)

    inbound1 = make_inbound_message(f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/handshake/get")
    route1 = parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, str(inbound1.topic))
    assert route1 is not None
    assert await dispatcher._handle_system_topic(route1, inbound1) is True
    assert ("publish_bridge_snapshot", ("handshake", inbound1)) in calls.items

    inbound2 = make_inbound_message(f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/summary/get")
    route2 = parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, str(inbound2.topic))
    assert route2 is not None
    assert await dispatcher._handle_system_topic(route2, inbound2) is True
    assert ("publish_bridge_snapshot", ("summary", inbound2)) in calls.items

    inbound3 = make_inbound_message(f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/nope")
    route3 = parse_topic(protocol.MQTT_DEFAULT_TOPIC_PREFIX, str(inbound3.topic))
    assert route3 is not None
    assert await dispatcher._handle_system_topic(route3, inbound3) is False


def test_pin_action_from_parts_variants() -> None:
    assert BridgeDispatcher._pin_action_from_parts([protocol.MQTT_DEFAULT_TOPIC_PREFIX, "digital"]) is None
    assert BridgeDispatcher._pin_action_from_parts([protocol.MQTT_DEFAULT_TOPIC_PREFIX, "digital", "13"]) == "write"
    assert BridgeDispatcher._pin_action_from_parts([protocol.MQTT_DEFAULT_TOPIC_PREFIX, "digital", "13", ""]) is None
    assert (
        BridgeDispatcher._pin_action_from_parts([protocol.MQTT_DEFAULT_TOPIC_PREFIX, "digital", "13", "READ"]) == "read"
    )


def test_payload_bytes_converts_supported_types_and_rejects_others() -> None:
    assert BridgeDispatcher._payload_bytes(b"a") == b"a"
    assert BridgeDispatcher._payload_bytes(bytearray(b"a")) == b"a"
    assert BridgeDispatcher._payload_bytes(memoryview(b"a")) == b"a"
    assert BridgeDispatcher._payload_bytes(None) == b""
    assert BridgeDispatcher._payload_bytes("hi") == b"hi"
    assert BridgeDispatcher._payload_bytes(12) == b"12"
    assert BridgeDispatcher._payload_bytes(1.5) == b"1.5"
    with pytest.raises(TypeError):
        BridgeDispatcher._payload_bytes(object())


@pytest.mark.asyncio
async def test_unexpected_mcu_gpio_requests_drop_if_pin_missing() -> None:
    calls = _Calls([])
    dispatcher = _make_dispatcher(calls)
    dispatcher.pin = None
    assert await dispatcher._handle_unexpected_digital_read(b"") is False
    assert await dispatcher._handle_unexpected_analog_read(b"") is False
