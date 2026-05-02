"""Command-matrix parity checks.

These tests ensure that the generated protocol routing data (MQTT subscriptions)
matches what the Python dispatcher actually handles.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcubridge.protocol.protocol import (
    MQTT_COMMAND_SUBSCRIPTIONS,
    Command,
    Status,
    expected_responses,
)
from mcubridge.protocol.topics import TopicRoute, parse_topic, topic_path
from mcubridge.services.dispatcher import BridgeDispatcher

_MQTT_PREFIX = "br"


def _parse_inbound_topic(topic: str) -> TopicRoute | None:
    return parse_topic(_MQTT_PREFIX, topic)


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

    mcu_registry: dict[int, Any] = {}

    from mcubridge.config.settings import get_default_config
    from mcubridge.state.context import create_runtime_state

    state = create_runtime_state(get_default_config())
    try:
        dispatcher = BridgeDispatcher(
            mcu_registry=mcu_registry,
            state=state,
            send_frame=AsyncMock(return_value=True),
            acknowledge_frame=AsyncMock(),
            is_topic_action_allowed=MagicMock(return_value=True),
            reject_topic_action=AsyncMock(),
            publish_bridge_snapshot=AsyncMock(),
        )

        # [SIL-2] Pass components directly to dispatcher
        components = {
            "console": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "datastore": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "file": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "mailbox": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "pin": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "process": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "spi": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "system": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
        }
        dispatcher.register_components(**components)

        for topic_enum, pattern, _qos in MQTT_COMMAND_SUBSCRIPTIONS:
            concrete = _materialize_subscription_segments(pattern)
            topic = topic_path(_MQTT_PREFIX, topic_enum, *concrete)
            route = _parse_inbound_topic(topic)
            assert route is not None, f"Failed to parse subscribed topic: {topic}"

            from aiomqtt.message import Message

            inbound: Any = MagicMock(spec=Message)
            inbound.topic = topic
            inbound.payload = b"hello"
            inbound.properties = None

            # [SIL-2] Using dispatch_mqtt_message directly
            await dispatcher.dispatch_mqtt_message(inbound, _parse_inbound_topic)
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_mcu_inbound_commands_are_registered() -> None:
    """MCU->Linux command IDs must always have registered handlers.

    This guards against protocol drift: adding a new command to the generated
    protocol enum should require a corresponding dispatcher/handler update.
    """

    mcu_registry: dict[int, Any] = {}
    AsyncMock()

    from mcubridge.config.settings import get_default_config
    from mcubridge.state.context import create_runtime_state

    state = create_runtime_state(get_default_config())
    try:
        dispatcher = BridgeDispatcher(
            mcu_registry=mcu_registry,
            state=state,
            send_frame=AsyncMock(return_value=True),
            acknowledge_frame=AsyncMock(),
            is_topic_action_allowed=MagicMock(return_value=True),
            reject_topic_action=AsyncMock(),
            publish_bridge_snapshot=AsyncMock(),
        )

        # [SIL-2] Pass components directly to dispatcher
        components = {
            "console": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "datastore": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "file": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "mailbox": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "pin": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "process": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "spi": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
            "system": MagicMock(handle_mqtt=AsyncMock(return_value=True)),
        }
        dispatcher.register_components(**components)

        dispatcher.register_system_handlers(
            handle_link_sync_resp=AsyncMock(return_value=True),
            handle_link_reset_resp=AsyncMock(return_value=True),
            handle_get_capabilities_resp=AsyncMock(return_value=True),
            handle_ack=AsyncMock(),
            status_handler_factory=MagicMock(return_value=AsyncMock()),
            handle_process_kill=AsyncMock(return_value=True),
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
