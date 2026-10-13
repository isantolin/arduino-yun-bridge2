"""Unit tests for mcubridge-gateway dynamic RPC forwarding."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway import GatewayLocalBridgeService, GatewayDaemon


@pytest.fixture
def mock_daemon_client():
    client = MagicMock()
    client.SetPinMode = AsyncMock(return_value={"status": "OK", "pin": 13, "mode": "OUTPUT"})
    client.DigitalWrite = AsyncMock(return_value={"status": "OK", "pin": 13, "val": 1})
    client.FileRead = AsyncMock(return_value={"status": "OK", "data": b"hello"})
    client.CustomUnknownRPC = AsyncMock(return_value={"status": "OK", "custom": True})
    return client


@pytest.mark.asyncio
async def test_forward_rpc_explicit_calls(mock_daemon_client):
    service = GatewayLocalBridgeService(daemon_client=mock_daemon_client)

    res_pin = await service.SetPinMode({"pin": 13, "mode": "OUTPUT"})
    assert res_pin == {"status": "OK", "pin": 13, "mode": "OUTPUT"}
    mock_daemon_client.SetPinMode.assert_awaited_once_with({"pin": 13, "mode": "OUTPUT"}, context=None)

    res_write = await service.DigitalWrite({"pin": 13, "val": 1})
    assert res_write == {"status": "OK", "pin": 13, "val": 1}
    mock_daemon_client.DigitalWrite.assert_awaited_once_with({"pin": 13, "val": 1}, context=None)


@pytest.mark.asyncio
async def test_dynamic_dispatch_any_rpc_method(mock_daemon_client):
    service = GatewayLocalBridgeService(daemon_client=mock_daemon_client)

    # Calling an arbitrary method not explicitly defined on the class
    res = await service.CustomUnknownRPC({"req": "test"})
    assert res == {"status": "OK", "custom": True}
    mock_daemon_client.CustomUnknownRPC.assert_awaited_once_with({"req": "test"}, context=None)


@pytest.mark.asyncio
async def test_unsupported_method_raises_attribute_error(mock_daemon_client):
    service = GatewayLocalBridgeService(daemon_client=mock_daemon_client)

    with pytest.raises(AttributeError, match="Daemon client does not support method 'NonExistentMethod'"):
        await service.NonExistentMethod({"req": "test"})


@pytest.mark.asyncio
async def test_private_attribute_raises_attribute_error(mock_daemon_client):
    service = GatewayLocalBridgeService(daemon_client=mock_daemon_client)

    with pytest.raises(AttributeError):
        _ = service._non_existent_private_attr


@pytest.mark.asyncio
async def test_gateway_daemon_lifecycle(mock_daemon_client):
    daemon = GatewayDaemon()
    assert not daemon._running

    await daemon.start(mock_daemon_client)
    assert daemon._running
    assert daemon.service is not None

    await daemon.stop()
    assert not daemon._running
