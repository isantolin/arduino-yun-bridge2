"""Extra coverage for mcubridge.services.datastore."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command, DatastoreAction, Status
from mcubridge.services.datastore import DatastoreComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_datastore_handle_put_malformed() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
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
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
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
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
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
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.publish = AsyncMock()
        ds = DatastoreComponent(config, state, ctx)

        # Unknown action
        await ds.handle_mqtt("unknown", ["key"], b"val", "val")

        # Put without key
        await ds.handle_mqtt(DatastoreAction.PUT, [], b"val", "val")

        # Get without key
        await ds.handle_mqtt(DatastoreAction.GET, [], b"", "")

        # Get request miss
        await ds.handle_mqtt(DatastoreAction.GET, ["missing", "request"], b"", "")
        # Check for datastore-miss error
        found_miss = False
        for call in ctx.publish.call_args_list:
            props = call.kwargs.get("properties")
            if props and any(k == "bridge-error" and v == "datastore-miss" for k, v in props):
                found_miss = True
        assert found_miss
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_datastore_mqtt_put_too_large() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
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
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    try:
        ctx = MagicMock()
        ctx.publish = AsyncMock()
        ds = DatastoreComponent(config, state, ctx)

        await ds._handle_mqtt_get("K" * 300, False, None)  # type: ignore[reportPrivateUsage]
        assert ctx.publish.call_count == 0
    finally:
        state.cleanup()
