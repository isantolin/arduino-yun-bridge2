"""Extra coverage for mcubridge.services.datastore."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, DatastoreAction, Status
from mcubridge.protocol.topics import Topic
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.state.context import create_runtime_state

from tests._helpers import make_route, make_mqtt_msg


@pytest.mark.asyncio
async def test_datastore_handle_put_malformed() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock()
        ctx.publish = AsyncMock()
        ds = DatastoreComponent(config, state, ctx)
        # Truncated varint — invalid protobuf
        assert await ds.handle_put(0, b"\x80") is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_get_malformed() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock()
        ctx.publish = AsyncMock()
        ds = DatastoreComponent(config, state, ctx)
        # Truncated varint — invalid protobuf
        assert await ds.handle_get_request(0, b"\x80") is False
        ctx.send_frame.assert_called_with(Status.MALFORMED.value, b"data_get_malformed")
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_get_truncation() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.send_frame = AsyncMock()
        ctx.publish = AsyncMock()
        ds = DatastoreComponent(config, state, ctx)

        key = "long_val"
        state.datastore[key] = "A" * 300

        from mcubridge.protocol.structures import DatastoreGetPacket

        payload = DatastoreGetPacket(key=key).encode()

        await ds.handle_get_request(0, payload)
        # Verify the sent frame payload size (should be capped around 255 + prefix)
        args = ctx.send_frame.call_args[0]
        assert args[0] == Command.CMD_DATASTORE_GET_RESP.value
        assert len(args[1]) > 0  # 1 byte prefix + 255 data + potentially something else
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_handle_mqtt_edge_cases() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.publish = AsyncMock()
        ds = DatastoreComponent(config, state, ctx)

        # Unknown action
        await ds.handle_mqtt(
            make_route(Topic.DATASTORE, "unknown", "key"), make_mqtt_msg(b"val")
        )

        # Put without key
        await ds.handle_mqtt(
            make_route(Topic.DATASTORE, DatastoreAction.PUT), make_mqtt_msg(b"val")
        )

        # Get without key
        await ds.handle_mqtt(
            make_route(Topic.DATASTORE, DatastoreAction.GET), make_mqtt_msg(b"")
        )

        # Get request miss
        await ds.handle_mqtt(
            make_route(Topic.DATASTORE, DatastoreAction.GET, "missing", "request"),
            make_mqtt_msg(b""),
        )
        # Check for datastore-miss error
        found_miss = False
        for call in ctx.publish.call_args_list:
            props = call.kwargs.get("properties")
            if props and any(
                k == "bridge-error" and v == "datastore-miss" for k, v in props
            ):
                found_miss = True
        assert found_miss
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_mqtt_put_too_large() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.publish = AsyncMock()
        ds = DatastoreComponent(config, state, ctx)

        # Large key
        await ds._handle_mqtt_put("K" * 300, "val", None)  # type: ignore[reportPrivateUsage]
        assert "K" * 300 not in state.datastore

        # Large value
        await ds._handle_mqtt_put("key", "V" * 300, None)  # type: ignore[reportPrivateUsage]
        assert state.datastore.get("key") != "V" * 300
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_mqtt_get_too_large() -> None:
    import time
    import os

    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.publish = AsyncMock()
        ds = DatastoreComponent(config, state, ctx)

        await ds._handle_mqtt_get("K" * 300, False, None)  # type: ignore[reportPrivateUsage]
        assert ctx.publish.call_count == 0
    finally:
        state.cleanup()
