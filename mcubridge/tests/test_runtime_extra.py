"""Extra unit tests for BridgeService (SIL-2)."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state

from .conftest import make_test_config


@pytest.mark.asyncio
async def test_runtime_on_serial_connected_errors() -> None:
    config = RuntimeConfig(
        allow_non_tmp_paths=True,
        serial_shared_secret=b"secret_1234",
        file_system_root=f".tmp_tests/mcubridge-test-{os.getpid()}-{time.time_ns()}",
        allow_non_tmp_paths=True,  # Added to bypass flash protection
    )
    state = create_runtime_state(config)
    try:
        from mcubridge.mqtt.spool_manager import MqttSpoolManager

        service = BridgeService(config, state, MagicMock(spec=MqttSpoolManager))

        # Test error in handshake sync
        service.handshake_manager.synchronize = AsyncMock(
            side_effect=OSError("sync-fail")
        )
        await service.on_serial_connected()
        assert state.serial_decode_errors == 0  # Not a decode error
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_reject_topic_action_properties() -> None:
    config = RuntimeConfig(
        allow_non_tmp_paths=True,
        serial_shared_secret=b"secret_1234",
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        from mcubridge.mqtt.spool_manager import MqttSpoolManager

        service = BridgeService(config, state, MagicMock(spec=MqttSpoolManager))
        service.publish = AsyncMock()  # Mock direct publish

        from mcubridge.protocol.topics import Topic
        from .conftest import make_inbound_message

        inbound = make_inbound_message("br/system/cmd", b"")
        inbound.properties = MagicMock()
        inbound.properties.ResponseTopic = "resp"
        inbound.properties.CorrelationData = b"cid"

        await service._reject_topic_action(inbound, Topic.SYSTEM, "action")  # type: ignore

        assert service.publish.called
        _, kwargs = service.publish.call_args
        assert kwargs.get("reply_to") is inbound
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_publish_bridge_snapshot_handshake() -> None:
    config = RuntimeConfig(
        allow_non_tmp_paths=True,
        serial_shared_secret=b"secret_1234",
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(config)
    try:
        from mcubridge.mqtt.spool_manager import MqttSpoolManager

        service = BridgeService(config, state, MagicMock(spec=MqttSpoolManager))
        service.publish = AsyncMock()  # Mock direct publish

        from .conftest import make_inbound_message

        inbound = make_inbound_message("br/s/b/h/get", b"")
        inbound.properties = MagicMock()
        inbound.properties.ResponseTopic = "reply"

        await service._publish_bridge_snapshot("handshake", inbound)  # type: ignore

        assert service.publish.called
        _, kwargs = service.publish.call_args
        assert kwargs.get("reply_to") is inbound
    finally:
        state.cleanup()
