"""Extra coverage for mcubridge.services.runtime."""

from unittest.mock import patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_runtime_on_serial_connected_errors() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    # Mock failures
    with (
        patch.object(service, "sync_link", side_effect=RuntimeError("sync fail")),
        patch.object(service._system, "request_mcu_version", side_effect=RuntimeError("ver fail")),
        patch.object(service._console, "flush_queue", side_effect=RuntimeError("flush fail")),
    ):
        await service.on_serial_connected()
        # Should not raise


@pytest.mark.asyncio
async def test_runtime_on_serial_disconnected_with_pending() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    # Add pending reads
    from mcubridge.state.context import PendingPinRequest
    state.pending_digital_reads.append(PendingPinRequest(pin=13, reply_context=None))

    await service.on_serial_disconnected()
    assert len(state.pending_digital_reads) == 0


@pytest.mark.asyncio
async def test_runtime_enqueue_mqtt_saturated() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234", mqtt_queue_limit=1)
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    from mcubridge.mqtt.messages import QueuedPublish
    msg1 = QueuedPublish(topic_name="t1", payload=b"p1")
    msg2 = QueuedPublish(topic_name="t2", payload=b"p2")

    await service.enqueue_mqtt(msg1)
    # This should drop msg1 and spool it
    with patch("mcubridge.state.context.RuntimeState.stash_mqtt_message", return_value=True):
        await service.enqueue_mqtt(msg2)

    assert state.mqtt_publish_queue.qsize() == 1


@pytest.mark.asyncio
async def test_runtime_acknowledge_frame_no_sender() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    service = BridgeService(config, state)
    service._serial_sender = None

    await service._acknowledge_mcu_frame(Command.CMD_GET_VERSION.value)
    # Should log error and return


@pytest.mark.asyncio
async def test_runtime_handle_ack_fallback() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    service = BridgeService(config, state)

    # Payload valid length (2) but msgspec decode fails if it's not a valid struct
    # AckPacket is UINT16, so any 2 bytes is technically valid for UINT16_STRUCT.
    # Let's try to trigger a failure in AckPacket.decode.
    with patch("mcubridge.protocol.structures.AckPacket.decode", side_effect=ValueError):
        await service._handle_ack(b"\x00\x40")
        # Should use fallback UINT16_STRUCT.parse
