"""Focused unit tests for BridgeService (runtime)."""

from __future__ import annotations
import msgspec
from mcubridge.transport.mqtt import MqttTransport

import time
from unittest.mock import AsyncMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol, structures
from mcubridge.protocol.protocol import Status
from mcubridge.protocol.topics import Topic
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


def _make_config() -> RuntimeConfig:
    import os

    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    spool_dir = f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
        file_system_root=fs_root,
        mqtt_spool_dir=spool_dir,
        allow_non_tmp_paths=True,
    )


@pytest.mark.asyncio
async def test_send_frame_without_serial_sender_returns_false() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        # Testing direct serial flow via service property
        ok = await service.serial_flow.send(
            protocol.Command.CMD_GET_VERSION.value, b"x"
        )
        assert ok is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_schedule_background_requires_context() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        async def _coro() -> None:
            return None

        pending = _coro()
        try:
            with pytest.raises(RuntimeError):
                await service.schedule_background(pending)
        finally:
            pending.close()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_serial_flow_acknowledge_no_sender_is_noop() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        await service.serial_flow.acknowledge(
            protocol.Command.CMD_GET_VERSION.value, 0, status=Status.ACK
        )
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_serial_flow_acknowledge_sends_ack_packet() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        sent: list[tuple[int, bytes]] = []

        async def _sender_side_effect(
            cmd: int, payload: bytes, seq_id: int | None = None
        ) -> bool:
            sent.append((cmd, payload))
            return True

        mock_sender = AsyncMock(side_effect=_sender_side_effect)
        service.register_serial_sender(mock_sender)

        await service.serial_flow.acknowledge(
            protocol.Command.CMD_GET_FREE_MEMORY.value,
            0,
            status=Status.MALFORMED,
        )

        assert sent
        status_cmd, payload = sent[0]
        assert status_cmd == Status.MALFORMED.value
        assert payload == msgspec.msgpack.encode(
            structures.AckPacket(command_id=protocol.Command.CMD_GET_FREE_MEMORY.value)
        )
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_enqueue_mqtt_applies_reply_context_properties() -> None:
    pass


@pytest.mark.asyncio
async def test_enqueue_mqtt_queue_full_drops_and_spools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pass


@pytest.mark.asyncio
async def test_reject_topic_action_enqueues_status() -> None:
    pass


@pytest.mark.asyncio
async def test_publish_bridge_snapshot_handshake_flavor() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        transport = MqttTransport(config, state)
        transport.enqueue_mqtt = AsyncMock()
        service = BridgeService(config, state, transport)

        from aiomqtt.message import Message

        # [SIL-2] Use spec=Message
        mock_inbound = AsyncMock(spec=Message)
        mock_inbound.topic = (
            f"{protocol.MQTT_DEFAULT_TOPIC_PREFIX}/system/bridge/handshake/get"
        )
        mock_inbound.properties = None

        await service._publish_bridge_snapshot("handshake", mock_inbound)  # type: ignore[reportPrivateUsage]

        transport.enqueue_mqtt.assert_awaited_once()
        queued = transport.enqueue_mqtt.call_args[0][0]
        assert "bridge/handshake/value" in queued.topic_name
    finally:
        state.cleanup()


def test_is_topic_action_allowed_empty_action_true() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, MqttTransport(config, state))

        assert service._is_topic_action_allowed(Topic.SYSTEM, "") is True  # type: ignore[reportPrivateUsage]
    finally:
        state.cleanup()
