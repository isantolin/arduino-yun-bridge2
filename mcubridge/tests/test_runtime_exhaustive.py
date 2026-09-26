"""Exhaustive tests for runtime service lifecycle, MQTT routing, and error branches."""

from __future__ import annotations

from unittest.mock import AsyncMock

import mcubridge.protocol.mcubridge_pb2 as pb
import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Command
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport
from pytest_mock import MockerFixture


def _make_config() -> RuntimeConfig:
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testsharedsecret",
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def runtime_setup() -> tuple[BridgeService, RuntimeState, AsyncMock]:
    cfg = _make_config()
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(cfg, state, mock_serial)
    return service, state, mock_serial


@pytest.mark.asyncio
async def test_on_serial_connected_and_disconnected(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, state, _serial = runtime_setup

    async def _mock_sync() -> None:
        state.connection_fsm.synchronize()

    mocker.patch.object(service.handshake, "synchronize", side_effect=_mock_sync)
    mocker.patch.object(service, "_request_mcu_version", new_callable=AsyncMock)
    mocker.patch.object(service, "_flush_console_queue", new_callable=AsyncMock)

    await service.on_serial_connected()
    assert state.is_connected is True

    await service.on_serial_disconnected()
    assert state.is_disconnected is True


@pytest.mark.asyncio
async def test_handle_mcu_frame_pre_sync_denied(runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = runtime_setup
    assert not state.is_synchronized

    await service.handle_mcu_frame(Command.CMD_GET_VERSION.value, 1, b"")
    serial.send.assert_not_called()


@pytest.mark.asyncio
async def test_handle_mcu_frame_handshake_routing(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = runtime_setup
    mock_sync_resp = AsyncMock(return_value=True)
    service.mcu_registry[Command.CMD_LINK_SYNC_RESP.value] = mock_sync_resp
    setattr(service.handshake, "handle_link_sync_resp", mock_sync_resp)

    sync_pb = pb.LinkSync(nonce=b"123456789012", tag=b"tag")
    await service.handle_mcu_frame(Command.CMD_LINK_SYNC_RESP.value, 1, sync_pb.SerializeToString())
    mock_sync_resp.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_mcu_frame_rpc_handlers(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock], mocker: MockerFixture
) -> None:
    service, state, serial = runtime_setup
    state.connection_fsm.connect()
    state.connection_fsm.synchronize()
    serial.send.return_value = True

    # 1. MCU Mailbox Push (triggers enqueue_cloud_publish & acknowledge)
    mock_enqueue = mocker.patch.object(service, "enqueue_cloud_publish", new_callable=AsyncMock)
    mock_incoming = AsyncMock()
    state.mailbox_incoming_queue = mock_incoming
    req_mb = pb.MailboxPush(data=b"test_payload")
    await service.handle_mcu_frame(Command.CMD_MAILBOX_PUSH.value, 10, req_mb.SerializeToString())
    mock_enqueue.assert_awaited()
    serial.acknowledge.assert_awaited_once_with(Command.CMD_MAILBOX_PUSH.value, 10)
    mock_incoming.append.assert_awaited_once_with(b"test_payload")

    # 2. MCU Datastore Put
    mock_cache = AsyncMock()
    mock_cache.get.return_value = b"25.5"
    state.datastore_cache = mock_cache
    req_ds = pb.DatastorePut(key="temp", value=b"25.5")
    await service.handle_mcu_frame(Command.CMD_DATASTORE_PUT.value, 11, req_ds.SerializeToString())
    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("temp") == b"25.5"
    mock_cache.set.assert_awaited_once_with("temp", b"25.5")

    # 3. SPI Transfer Response
    mock_enqueue.reset_mock()
    spi_resp = pb.SpiTransferResponse(data=b"\x01\x02")
    await service.handle_mcu_frame(Command.CMD_SPI_TRANSFER_RESP.value, 12, spi_resp.SerializeToString())
    mock_enqueue.assert_awaited()


@pytest.mark.asyncio
async def test_handle_request_routing(runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock]) -> None:
    service, state, serial = runtime_setup
    state.connection_fsm.connect()
    state.connection_fsm.synchronize()
    serial.send.return_value = True

    # Console input topic
    req_console = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/console/in",
        payload=b"help\n",
    )
    await service.handle_request(req_console)
    assert len(state.console_to_mcu_queue) == 0  # Should be flushed immediately to serial
    serial.send.assert_awaited()

    # Mailbox write topic
    req_mb = pb.CloudQueuedPublish(
        topic_name=f"{state.cloud_topic_prefix}/mailbox/write",
        payload=b"ping",
    )
    await service.handle_request(req_mb)
    serial.send.assert_awaited()


@pytest.mark.asyncio
async def test_enqueue_cloud_spool_and_flush(
    runtime_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = runtime_setup

    # Enqueue when disconnected triggers spooling
    msg = pb.CloudQueuedPublish(
        topic_name="mcu/test",
        payload=b"spooled_data",
    )
    await service.enqueue_cloud(msg)
    spool = getattr(service, "_cloud_spool")
    assert spool is not None
    assert len(spool) >= 1

    # Drain spool
    popped_bytes = await spool.popleft()
    popped_msg = pb.CloudQueuedPublish.FromString(popped_bytes)
    assert popped_msg.topic_name == "mcu/test"
    assert popped_msg.payload == b"spooled_data"
