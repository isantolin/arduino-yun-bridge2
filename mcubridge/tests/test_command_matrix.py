"""Command-matrix parity checks.

These tests ensure that the generated protocol routing data (MQTT subscriptions)
matches what the Python dispatcher actually handles.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from mcubridge.protocol.protocol import (
    MQTT_COMMAND_SUBSCRIPTIONS,
    Command,
    Status,
    Topic,
    expected_responses,
)
from mcubridge.protocol.topics import TopicRoute, parse_topic, topic_path
from mcubridge.router.routers import MQTTRouter, McuHandler
from mcubridge.services.dispatcher import BridgeDispatcher

from .conftest import make_component_container


class _DummyMessage(msgspec.Struct):
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


async def _noop_link_handler(_seq_id: int, _payload: bytes) -> bool:
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

    mcu_registry: dict[int, McuHandler] = {}
    mqtt_router = MQTTRouter()

    from mcubridge.config.settings import get_default_config
    from mcubridge.state.context import create_runtime_state

    state = create_runtime_state(get_default_config())
    try:
        dispatcher = BridgeDispatcher(
            mcu_registry=mcu_registry,
            mqtt_router=mqtt_router,
            state=state,
            send_frame=_noop_send_frame,
            acknowledge_frame=_noop_acknowledge_frame,
            is_topic_action_allowed=_always_allowed,
            reject_topic_action=_noop_reject_topic_action,
            publish_bridge_snapshot=_noop_publish_bridge_snapshot,
        )

        # Mock components with specialized handlers
        console = MagicMock()
        console.handle_mqtt_in = AsyncMock(return_value=True)

        datastore = MagicMock()
        datastore.handle_mqtt_put = AsyncMock(return_value=True)
        datastore.handle_mqtt_get = AsyncMock(return_value=True)

        file = MagicMock()
        file.handle_mqtt_write = AsyncMock(return_value=True)
        file.handle_mqtt_read = AsyncMock(return_value=True)
        file.handle_mqtt_remove = AsyncMock(return_value=True)

        mailbox = MagicMock()
        mailbox.handle_mqtt_write = AsyncMock(return_value=True)
        mailbox.handle_mqtt_read = AsyncMock(return_value=True)

        pin = MagicMock()
        pin.handle_mqtt_write = AsyncMock(return_value=True)
        pin.handle_mqtt_read = AsyncMock(return_value=True)
        pin.handle_mqtt_mode = AsyncMock(return_value=True)

        process = MagicMock()
        process.handle_mqtt_run_async = AsyncMock(return_value=True)
        process.handle_mqtt_poll = AsyncMock(return_value=True)
        process.handle_mqtt_kill = AsyncMock(return_value=True)

        spi = MagicMock()
        spi.handle_mqtt_begin = AsyncMock(return_value=True)
        spi.handle_mqtt_end = AsyncMock(return_value=True)
        spi.handle_mqtt_config = AsyncMock(return_value=True)
        spi.handle_mqtt_transfer = AsyncMock(return_value=True)

        system = MagicMock()
        system.handle_mqtt_bootloader = AsyncMock(return_value=True)
        system.handle_mqtt_free_memory = AsyncMock(return_value=True)
        system.handle_mqtt_version = AsyncMock(return_value=True)
        system.handle_mqtt = AsyncMock(return_value=True)

        dispatcher.register_components(
            make_component_container(
                console=console,
                datastore=datastore,
                file=file,
                mailbox=mailbox,
                pin=pin,
                process=process,
                spi=spi,
                system=system,
            )
        )

        for topic_enum, pattern, _qos in MQTT_COMMAND_SUBSCRIPTIONS:
            concrete = _materialize_subscription_segments(pattern)
            topic = topic_path(_MQTT_PREFIX, topic_enum, *concrete)
            route = _parse_inbound_topic(topic)
            assert route is not None, f"Failed to parse subscribed topic: {topic}"

            inbound: Any = _DummyMessage(topic=topic, payload=b"hello")

            handled = await mqtt_router.dispatch(route, inbound)
            assert handled, f"No handler registered for subscribed topic: {topic}"
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mcu_inbound_commands_are_registered() -> None:
    """MCU->Linux command IDs must always have registered handlers.

    This guards against protocol drift: adding a new command to the generated
    protocol enum should require a corresponding dispatcher/handler update.
    """

    mcu_registry: dict[int, McuHandler] = {}
    mqtt_router = MQTTRouter()

    from mcubridge.config.settings import get_default_config
    from mcubridge.state.context import create_runtime_state

    state = create_runtime_state(get_default_config())
    try:
        dispatcher = BridgeDispatcher(
            mcu_registry=mcu_registry,
            mqtt_router=mqtt_router,
            state=state,
            send_frame=_noop_send_frame,
            acknowledge_frame=_noop_acknowledge_frame,
            is_topic_action_allowed=_always_allowed,
            reject_topic_action=_noop_reject_topic_action,
            publish_bridge_snapshot=_noop_publish_bridge_snapshot,
        )

        dispatcher.register_components(
            make_component_container(
                console=MagicMock(),
                datastore=MagicMock(),
                file=MagicMock(),
                mailbox=MagicMock(),
                pin=MagicMock(),
                process=MagicMock(),
                spi=MagicMock(),
                system=MagicMock(),
            )
        )
        dispatcher.register_system_handlers(
            handle_link_sync_resp=_noop_link_handler,
            handle_link_reset_resp=_noop_link_handler,
            handle_get_capabilities_resp=_noop_link_handler,
            handle_ack=_noop_ack_handler,  # type: ignore[reportArgumentType]
            status_handler_factory=_noop_status_handler_factory,  # type: ignore[reportArgumentType]
            handle_process_kill=_noop_process_kill,  # type: ignore[reportArgumentType]
        )

        # Commands initiated by Linux (to the MCU). These requests should not be
        # required as inbound handlers (though some may have "unexpected" handlers).
        linux_to_mcu_requests: frozenset[int] = frozenset(
            {
                Command.CMD_GET_VERSION.value,
                Command.CMD_GET_FREE_MEMORY.value,
                Command.CMD_GET_CAPABILITIES.value,
                Command.CMD_LINK_SYNC.value,
                Command.CMD_LINK_RESET.value,
                Command.CMD_SET_BAUDRATE.value,
                Command.CMD_SET_PIN_MODE.value,
                Command.CMD_DIGITAL_WRITE.value,
                Command.CMD_ANALOG_WRITE.value,
                Command.CMD_DIGITAL_READ.value,
                Command.CMD_ANALOG_READ.value,
                Command.CMD_ENTER_BOOTLOADER.value,
                Command.CMD_SPI_BEGIN.value,
                Command.CMD_SPI_TRANSFER.value,
                Command.CMD_SPI_END.value,
                Command.CMD_SPI_SET_CONFIG.value,
                Command.CMD_SET_BAUDRATE_RESP.value,
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

        assert (
            not missing_status
        ), f"Missing status handler registrations: {missing_status}"
    finally:
        state.cleanup()
