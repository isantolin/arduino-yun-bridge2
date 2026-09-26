from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from grpclib.client import Channel
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge_client import LocalBridgeStub
from mcubridge_client.definitions import build_bridge_args
from mcubridge_client.env import dump_client_env, is_openwrt, read_uci_general
from mcubridge_client.spi import SpiDevice
from pytest_mock import MockerFixture


@pytest.fixture
def mock_grpc() -> tuple[MagicMock, MagicMock]:
    mock_channel = MagicMock(spec=Channel)

    mock_stub = MagicMock(spec=LocalBridgeStub(mock_channel))
    mock_stub.DigitalWrite = AsyncMock()
    mock_stub.AnalogWrite = AsyncMock()
    mock_stub.DatastorePut = AsyncMock()
    mock_stub.FileWrite = AsyncMock()
    mock_stub.AnalogRead = AsyncMock()
    mock_stub.PinSubscribe = AsyncMock()
    return mock_channel, mock_stub


@pytest.mark.asyncio
async def test_client_connect_disconnect(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    mock_channel, mock_stub = mock_grpc
    assert mock_channel is not None
    assert mock_stub is not None


@pytest.mark.asyncio
async def test_client_digital_write(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    msg = pb.DigitalWrite(pin=13, value=1)
    await mock_stub.DigitalWrite(msg)
    mock_fn = cast(AsyncMock, mock_stub.DigitalWrite)
    mock_fn.assert_awaited_once_with(msg)
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(pb.DigitalWrite, call_args[0][0])
    assert sent.pin == 13
    assert sent.value == 1


@pytest.mark.asyncio
async def test_client_analog_write(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    msg = pb.AnalogWrite(pin=3, value=128)
    await mock_stub.AnalogWrite(msg)
    mock_fn = cast(AsyncMock, mock_stub.AnalogWrite)
    mock_fn.assert_awaited_once_with(msg)
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(pb.AnalogWrite, call_args[0][0])
    assert sent.pin == 3
    assert sent.value == 128


@pytest.mark.asyncio
async def test_client_datastore_put(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    mock_fn = cast(AsyncMock, mock_stub.DatastorePut)
    mock_fn.return_value = pb.GenericResponse(status="ok")
    msg = pb.DatastorePut(key="test_key", value=b"test_value")
    res = cast(pb.GenericResponse, await mock_stub.DatastorePut(msg))
    mock_fn.assert_awaited_once_with(msg)
    assert res.status == "ok"
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(pb.DatastorePut, call_args[0][0])
    assert sent.key == "test_key"
    assert sent.value == b"test_value"


@pytest.mark.asyncio
async def test_client_file_write(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    msg = pb.FileWrite(path="test.txt", data=b"content")
    await mock_stub.FileWrite(msg)
    mock_fn = cast(AsyncMock, mock_stub.FileWrite)
    mock_fn.assert_awaited_once_with(msg)
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(pb.FileWrite, call_args[0][0])
    assert sent.path == "test.txt"
    assert sent.data == b"content"


@pytest.mark.asyncio
async def test_client_analog_read_timeout(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    mock_fn = cast(AsyncMock, mock_stub.AnalogRead)
    mock_fn.side_effect = TimeoutError("RPC timeout")
    msg = pb.PinRead(pin=0)
    with pytest.raises(TimeoutError):
        await mock_stub.AnalogRead(msg)


@pytest.mark.asyncio
async def test_client_pin_subscribe(mock_grpc: tuple[MagicMock, MagicMock]) -> None:
    _, mock_stub = mock_grpc
    mock_fn = cast(AsyncMock, mock_stub.PinSubscribe)
    mock_fn.return_value = pb.PinSubscribeResponse(pin=13, success=True)
    msg = pb.PinSubscribeRequest(
        pin=13,
        mode=pb.PinModeType.PIN_INPUT,
        interval_ms=100,
        hysteresis=1,
        enabled=True,
    )
    res = cast(pb.PinSubscribeResponse, await mock_stub.PinSubscribe(msg))
    mock_fn.assert_awaited_once_with(msg)
    assert res.pin == 13
    assert res.success is True
    call_args = mock_fn.call_args
    assert call_args is not None
    sent = cast(pb.PinSubscribeRequest, call_args[0][0])
    assert sent.pin == 13
    assert sent.interval_ms == 100
    assert sent.enabled is True


def test_definitions_build_bridge_args() -> None:
    # With topic_prefix
    args1 = build_bridge_args(host="10.0.0.1", port=9000, device_id="dev1", topic_prefix="custom")
    assert args1["topic_prefix"] == "custom"
    assert args1["device_id"] == "dev1"

    # Without topic_prefix (empty string triggers False branch)
    args2 = build_bridge_args(host="10.0.0.1", port=9000, device_id="dev1", topic_prefix="")
    assert "topic_prefix" not in args2
    assert args2["device_id"] == "dev1"


def test_env_functions(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    import importlib.util

    # 1. is_openwrt with forced env
    monkeypatch.setenv("MCUBRIDGE_FORCE_UCI", "1")
    assert is_openwrt() is True

    # 2. read_uci_general when find_spec is None
    mocker.patch.object(importlib.util, "find_spec", return_value=None)
    assert read_uci_general() == {}

    # 3. read_uci_general when get_uci_config is not callable
    fake_mod = MagicMock()
    fake_mod.get_uci_config = "not_callable"
    mocker.patch.object(importlib.util, "find_spec", return_value=MagicMock())
    mocker.patch("importlib.import_module", return_value=fake_mod)
    assert read_uci_general() == {}

    # 4. read_uci_general when get_uci_config raises OSError
    fake_mod.get_uci_config = MagicMock(side_effect=OSError("disk error"))
    assert read_uci_general() == {}

    # 5. read_uci_general with valid config filtering internal keys
    fake_mod.get_uci_config = MagicMock(return_value={"port": "50051", ".hidden": "x", "_priv": "y"})
    res = read_uci_general()
    assert res == {"port": "50051"}

    # 6. dump_client_env
    logs: list[str] = []
    fake_logger = MagicMock()

    def _capture_log(msg: str) -> None:
        logs.append(msg)

    fake_logger.info = _capture_log
    dump_client_env(fake_logger)
    assert len(logs) == 4
    assert "MCU Bridge client configuration" in logs[0]


@pytest.mark.asyncio
async def test_spi_device_transfer() -> None:
    mock_channel = MagicMock(spec=Channel)

    mock_stub = MagicMock(spec=LocalBridgeStub(mock_channel))
    setattr(mock_stub, "SpiConfigure", AsyncMock())
    setattr(mock_stub, "SpiTransfer", AsyncMock(return_value=pb.SpiTransfer(data=b"\xaa\xbb")))

    dev = SpiDevice(mock_stub)

    # Transfer with bytes while inactive (should call begin() automatically)
    assert not getattr(dev, "_active")
    res1 = await dev.transfer(b"\x01\x02")
    assert getattr(dev, "_active") is True
    assert res1 == b"\xaa\xbb"
    cast(AsyncMock, mock_stub.SpiConfigure).assert_awaited_once()

    # Transfer with sequence/list of ints while active
    res2 = await dev.transfer([0x03, 0x04])
    assert res2 == b"\xaa\xbb"

    # End
    await dev.end()
    assert not getattr(dev, "_active")
