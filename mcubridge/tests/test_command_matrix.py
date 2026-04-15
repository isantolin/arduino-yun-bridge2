"""Command-matrix parity checks.

These tests ensure that the generated protocol routing data (MQTT subscriptions)
matches what the Python service actually handles.
"""

from __future__ import annotations

import unittest.mock
from typing import Any, cast

import msgspec
import pytest

from mcubridge.config.settings import RuntimeConfig, get_default_config
from mcubridge.protocol.contracts import expected_responses
from mcubridge.protocol.protocol import (
    MQTT_COMMAND_SUBSCRIPTIONS,
    Command,
    Status,
)
from mcubridge.protocol.topics import TopicRoute, parse_topic, topic_path
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


class _DummyMessage(msgspec.Struct):
    topic: str
    payload: bytes = b""
    properties: Any = None


_MQTT_PREFIX = "br"


def _parse_inbound_topic(topic: str) -> TopicRoute | None:
    return parse_topic(_MQTT_PREFIX, topic)


class _NoopComponent:
    async def handle_mqtt(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        if name.startswith("handle_") or name.startswith("on_"):

            async def _noop(*args: Any, **kwargs: Any) -> bool:
                return True

            return _noop
        raise AttributeError(name)


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
    """Every subscribed MQTT topic pattern is accepted by the service."""

    config = cast(RuntimeConfig, get_default_config())
    state = create_runtime_state(config)

    # We mock the internal registry to avoid real component instantiation
    with unittest.mock.patch("svcs.Container.get", return_value=_NoopComponent()):
        service = BridgeService(config, state)

        for topic_enum, pattern, _qos in MQTT_COMMAND_SUBSCRIPTIONS:
            concrete = _materialize_subscription_segments(pattern)
            topic = topic_path(_MQTT_PREFIX, topic_enum, *concrete)
            route = _parse_inbound_topic(topic)
            assert route is not None, f"Failed to parse subscribed topic: {topic}"

            inbound: Any = _DummyMessage(topic=topic, payload=b"hello")

            handled = await service._mqtt_router.dispatch(  # type: ignore[reportPrivateUsage]
                route, inbound
            )
            assert handled, f"No handler registered for subscribed topic: {topic}"

    state.cleanup()


@pytest.mark.asyncio
async def test_mcu_inbound_commands_are_registered() -> None:
    """MCU->Linux command IDs must always have registered handlers."""

    config = cast(RuntimeConfig, get_default_config())
    state = create_runtime_state(config)

    with unittest.mock.patch("svcs.Container.get", return_value=_NoopComponent()):
        service = BridgeService(config, state)
        mcu_registry = service._mcu_registry  # type: ignore[reportPrivateUsage]

        # Commands initiated by Linux (to the MCU).
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

    state.cleanup()
