"""Extra coverage for mcubridge.services.runtime."""

from unittest.mock import patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command
from mcubridge.services import ConsoleComponent, SystemComponent
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_runtime_on_serial_connected_errors() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)
        system = service.container.get(SystemComponent)
        console = service.container.get(ConsoleComponent)

        # Mock failures
        with (
            patch.object(service, "sync_link", side_effect=RuntimeError("sync fail")),
            patch.object(
                system, "request_mcu_version", side_effect=RuntimeError("ver fail")
            ),
            patch.object(
                console, "flush_queue", side_effect=RuntimeError("flush fail")
            ),
        ):
            await service.on_serial_connected()
            # Should not raise
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_on_serial_disconnected_with_pending() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)

        # Add pending reads
        from mcubridge.state.context import PendingPinRequest

        state.pending_digital_reads.append(
            PendingPinRequest(pin=13, reply_context=None)
        )

        await service.on_serial_disconnected()
        assert len(state.pending_digital_reads) == 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_enqueue_mqtt_saturated() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234", mqtt_queue_limit=1)
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)

        from mcubridge.protocol.structures import QueuedPublish

        msg1 = QueuedPublish(topic_name="t1", payload=b"p1")
        msg2 = QueuedPublish(topic_name="t2", payload=b"p2")

        await service.enqueue_mqtt(msg1)
        # This should drop msg1 and spool it
        with patch(
            "mcubridge.state.context.RuntimeState.stash_mqtt_message", return_value=True
        ):
            await service.enqueue_mqtt(msg2)

        assert state.mqtt_publish_queue.qsize() == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_acknowledge_frame_no_sender() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)
        service.serial_sender = None

        await service.acknowledge_mcu_frame(Command.CMD_GET_VERSION.value, 0)
        # Should log error and return
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_runtime_handle_ack_fallback() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state)

        # Payload valid length (2) but decode may fail for malformed data.
        # AckPacket is a protobuf message with a single uint32 field.
        # Let's try to trigger a failure in AckPacket.decode.
        with patch(
            "mcubridge.protocol.structures.AckPacket.decode", side_effect=ValueError
        ):
            await service.handle_ack(0, b"\x00\x40")
            # Should handle the decode failure gracefully
    finally:
        state.cleanup()
