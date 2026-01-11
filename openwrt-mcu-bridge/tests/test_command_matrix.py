"""Command-matrix parity checks.

These tests ensure that the generated protocol routing data (MQTT subscriptions)
matches what the Python dispatcher actually handles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, cast

import pytest

from mcubridge.protocol.topics import TopicRoute, parse_topic, topic_path
from mcubridge.rpc.contracts import expected_responses
from mcubridge.rpc.protocol import MQTT_COMMAND_SUBSCRIPTIONS, Command, Status, Topic
from mcubridge.services.dispatcher import BridgeDispatcher
from mcubridge.services.routers import MCUHandlerRegistry, MQTTRouter


@dataclass(slots=True)
class _DummyMessage:
    topic: str
    payload: bytes = b""
    properties: Any = None


_MQTT_PREFIX = "br"


def _parse_inbound_topic(topic: str) -> TopicRoute | None:
    return parse_topic(_MQTT_PREFIX, topic)


async def _noop_send_frame(_command_id: int, _payload: bytes) -> bool:
    return True


async def _noop_acknowledge_frame(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _noop_reject_topic_action(
    _inbound: Any,
    _topic: Topic | str,
    _action: str,
) -> None:
    return None


async def _noop_publish_bridge_snapshot(
    _kind: str,
    _inbound: Any,
) -> None:
    return None


async def _noop_link_handler(_payload: bytes) -> bool:
    return True


async def _noop_ack_handler(_payload: bytes) -> None:
    return None


async def _noop_process_kill(_payload: bytes) -> bool:
    return True


async def _noop_status_handler(_payload: bytes) -> None:
    return None


def _noop_status_handler_factory(_status: Status) -> Callable[[bytes], Any]:
    return _noop_status_handler


def _always_allowed(_topic: Topic | str, _action: str) -> bool:
    return True


def _link_synchronized() -> bool:
    return True


class _Console:
    async def handle_xoff(self, _payload: bytes) -> bool:
        return True

    async def handle_xon(self, _payload: bytes) -> bool:
        return True

    async def handle_write(self, _payload: bytes) -> bool:
        return True

    async def handle_mqtt_input(self, _payload: bytes, _inbound: _DummyMessage) -> bool:
        return True


class _Datastore:
    async def handle_put(self, _payload: bytes) -> bool:
        return True

    async def handle_get_request(self, _payload: bytes) -> bool:
        return True

    async def handle_mqtt(
        self,
        _identifier: str,
        _remainder: list[str],
        _payload: bytes,
        _payload_str: str,
        _inbound: _DummyMessage,
    ) -> bool:
        return True


class _File:
    async def handle_write(self, _payload: bytes) -> bool:
        return True

    async def handle_read(self, _payload: bytes) -> bool:
        return True

    async def handle_remove(self, _payload: bytes) -> bool:
        return True

    async def handle_mqtt(
        self,
        _identifier: str,
        _remainder: list[str],
        _payload: bytes,
        _inbound: _DummyMessage,
    ) -> bool:
        return True


class _Mailbox:
    async def handle_push(self, _payload: bytes) -> bool:
        return True

    async def handle_available(self, _payload: bytes) -> bool:
        return True

    async def handle_read(self, _payload: bytes) -> bool:
        return True

    async def handle_processed(self, _payload: bytes) -> bool:
        return True

    async def handle_mqtt(
        self,
        _identifier: str,
        _payload: bytes,
        _inbound: _DummyMessage,
    ) -> bool:
        return True


class _Pin:
    async def handle_digital_read_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_analog_read_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_unexpected_mcu_request(self, _command: Any, _payload: bytes) -> bool:
        return True

    async def handle_mqtt(
        self,
        _topic: Topic,
        _parts: list[str],
        _payload_str: str,
        _inbound: _DummyMessage,
    ) -> bool:
        return True


class _Process:
    async def handle_run(self, _payload: bytes) -> bool:
        return True

    async def handle_run_async(self, _payload: bytes) -> bool:
        return True

    async def handle_poll(self, _payload: bytes) -> bool:
        return True


class _Shell:
    async def handle_mqtt(
        self,
        _parts: list[str],
        _payload: bytes,
        _inbound: _DummyMessage,
    ) -> bool:
        return True


class _System:
    async def handle_get_free_memory_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_get_version_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_set_baudrate_resp(self, _payload: bytes) -> bool:
        return True

    async def handle_mqtt(
        self,
        _identifier: str,
        _remainder: list[str],
        _inbound: _DummyMessage,
    ) -> bool:
        return True


def _materialize_subscription_segments(pattern: tuple[str, ...]) -> tuple[str, ...]:
    rendered: list[str] = []
    for segment in pattern:
        if segment == "+":
            rendered.append("13")
        elif segment == "#":
            rendered.extend(["path", "to", "key"])
        else:
            rendered.append(segment)
    return tuple(rendered)


@pytest.mark.asyncio
async def test_mqtt_subscriptions_are_dispatched() -> None:
    """Every subscribed MQTT topic pattern is accepted by the dispatcher."""

    mcu_registry = MCUHandlerRegistry()
    mqtt_router = MQTTRouter()

    dispatcher = BridgeDispatcher(
        mcu_registry=mcu_registry,
        mqtt_router=mqtt_router,
        send_frame=_noop_send_frame,
        acknowledge_frame=_noop_acknowledge_frame,
        is_link_synchronized=_link_synchronized,
        is_topic_action_allowed=_always_allowed,
        reject_topic_action=_noop_reject_topic_action,
        publish_bridge_snapshot=_noop_publish_bridge_snapshot,
    )

    dispatcher.register_components(
        console=cast(Any, _Console()),
        datastore=cast(Any, _Datastore()),
        file=cast(Any, _File()),
        mailbox=cast(Any, _Mailbox()),
        pin=cast(Any, _Pin()),
        process=cast(Any, _Process()),
        shell=cast(Any, _Shell()),
        system=cast(Any, _System()),
    )

    for topic_enum, pattern, _qos in MQTT_COMMAND_SUBSCRIPTIONS:
        concrete = _materialize_subscription_segments(pattern)
        topic = topic_path(_MQTT_PREFIX, topic_enum, *concrete)
        route = _parse_inbound_topic(topic)
        assert route is not None, f"Failed to parse subscribed topic: {topic}"

        inbound: Any = _DummyMessage(topic=topic, payload=b"hello")

        handled = await mqtt_router.dispatch(route, inbound)
        assert handled, f"No handler registered for subscribed topic: {topic}"


@pytest.mark.asyncio
async def test_mcu_inbound_commands_are_registered() -> None:
    """MCU->Linux command IDs must always have registered handlers.

    This guards against protocol drift: adding a new command to the generated
    protocol enum should require a corresponding dispatcher/handler update.
    """

    mcu_registry = MCUHandlerRegistry()
    mqtt_router = MQTTRouter()

    dispatcher = BridgeDispatcher(
        mcu_registry=mcu_registry,
        mqtt_router=mqtt_router,
        send_frame=_noop_send_frame,
        acknowledge_frame=_noop_acknowledge_frame,
        is_link_synchronized=_link_synchronized,
        is_topic_action_allowed=_always_allowed,
        reject_topic_action=_noop_reject_topic_action,
        publish_bridge_snapshot=_noop_publish_bridge_snapshot,
    )

    dispatcher.register_components(
        console=cast(Any, _Console()),
        datastore=cast(Any, _Datastore()),
        file=cast(Any, _File()),
        mailbox=cast(Any, _Mailbox()),
        pin=cast(Any, _Pin()),
        process=cast(Any, _Process()),
        shell=cast(Any, _Shell()),
        system=cast(Any, _System()),
    )
    dispatcher.register_system_handlers(
        handle_link_sync_resp=_noop_link_handler,
        handle_link_reset_resp=_noop_link_handler,
        handle_ack=_noop_ack_handler,
        status_handler_factory=_noop_status_handler_factory,
        handle_process_kill=_noop_process_kill,
    )

    # Commands initiated by Linux (to the MCU). These requests should not be
    # required as inbound handlers (though some may have "unexpected" handlers).
    linux_to_mcu_requests: frozenset[int] = frozenset(
        {
            Command.CMD_GET_VERSION.value,
            Command.CMD_GET_FREE_MEMORY.value,
            Command.CMD_LINK_SYNC.value,
            Command.CMD_LINK_RESET.value,
            Command.CMD_SET_BAUDRATE.value,
            Command.CMD_SET_PIN_MODE.value,
            Command.CMD_DIGITAL_WRITE.value,
            Command.CMD_ANALOG_WRITE.value,
            Command.CMD_DIGITAL_READ.value,
            Command.CMD_ANALOG_READ.value,
        }
    )

    mcu_to_linux_requests: set[int] = set()
    for cmd in Command:
        if cmd.name.endswith("_RESP"):
            continue
        if cmd.value in linux_to_mcu_requests:
            continue
        mcu_to_linux_requests.add(cmd.value)

    outbound_only_responses: set[int] = set()
    for request_id in mcu_to_linux_requests:
        outbound_only_responses.update(expected_responses(request_id))

    required_commands: set[int] = set()
    for cmd in Command:
        if cmd.value in linux_to_mcu_requests:
            continue
        if cmd.value in outbound_only_responses:
            continue
        required_commands.add(cmd.value)

    missing: list[str] = []
    for cmd in sorted(required_commands):
        if mcu_registry.get(cmd) is None:
            try:
                name = Command(cmd).name
            except ValueError:
                name = f"0x{cmd:02X}"
            missing.append(name)

    assert not missing, f"Missing MCU handler registrations: {missing}"

    # Status frames are also inbound and must always be registered.
    missing_status: list[str] = []
    for status in Status:
        if mcu_registry.get(status.value) is None:
            missing_status.append(status.name)

    assert not missing_status, f"Missing status handler registrations: {missing_status}"
