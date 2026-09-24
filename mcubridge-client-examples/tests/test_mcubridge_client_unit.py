"""Unit tests for mcubridge_client library components. [SIL-2]"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge_client import (
    DEFAULT_TOPIC_PREFIX,
    LocalBridgeStub,
    SpiBitOrder,
    SpiDevice,
    SpiMode,
    Topic,
    build_bridge_args,
    dump_client_env,
)
from mcubridge_client.cli import bridge_session
from mcubridge_client.env import is_openwrt, read_uci_general

# ==============================================================================
# cli.py & env.py tests
# ==============================================================================


@pytest.mark.asyncio
async def test_cli_bridge_session(mocker: MockerFixture) -> None:
    """bridge_session context manager yields Channel and LocalBridgeStub."""
    mock_chan_cls = mocker.patch("mcubridge_client.cli.Channel")
    mock_stub_cls = mocker.patch("mcubridge_client.cli.LocalBridgeStub")
    mock_chan = MagicMock()
    mock_chan.__dispatch__ = MagicMock()
    mock_stub = MagicMock()
    mock_chan_cls.return_value = mock_chan
    mock_stub_cls.return_value = mock_stub

    async with bridge_session(host="127.0.0.1", port=8443, device_id="yun-01", topic_prefix="br") as (
        chan,
        stub,
    ):
        assert chan is mock_chan
        assert stub is mock_stub


def test_env_is_openwrt(mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """is_openwrt checks environment variable and file presence."""
    monkeypatch.setenv("MCUBRIDGE_FORCE_UCI", "1")
    assert is_openwrt() is True

    monkeypatch.delenv("MCUBRIDGE_FORCE_UCI", raising=False)
    mocker.patch("pathlib.Path.exists", return_value=True)
    assert is_openwrt() is True


def test_env_read_uci_general(mocker: MockerFixture) -> None:
    """read_uci_general returns UCI config dict or empty dict."""
    mocker.patch("mcubridge_client.env.is_openwrt", return_value=False)
    assert read_uci_general() == {}

    mocker.patch("mcubridge_client.env.is_openwrt", return_value=True)
    mocker.patch("importlib.util.find_spec", return_value=MagicMock())
    mock_imp = mocker.patch("importlib.import_module")
    mock_mod = MagicMock()
    mock_mod.get_uci_config = MagicMock(return_value={"cloud_host": "127.0.0.1", "_private": "x"})
    mock_imp.return_value = mock_mod
    res = read_uci_general()
    assert res == {"cloud_host": "127.0.0.1"}

    # Exception path in get_uci_config
    mock_mod.get_uci_config.side_effect = RuntimeError("UCI error")
    assert read_uci_general() == {}

    # Exception path in import_module
    mock_imp.side_effect = ImportError("No module")
    assert read_uci_general() == {}


def test_env_dump_client_env(capsys: pytest.CaptureFixture[str]) -> None:
    """dump_client_env logs or prints client settings."""
    # 1. Custom logger path
    mock_logger = MagicMock()
    dump_client_env(mock_logger)
    assert mock_logger.debug.called

    # 2. Stdout fallback
    dump_client_env(None)
    captured = capsys.readouterr()
    assert "gateway_host=" in captured.out
    assert "target_device_id=" in captured.out


# ==============================================================================
# definitions.py & spi.py tests
# ==============================================================================


def test_definitions_build_bridge_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_bridge_args builds dictionary targeting Gateway with explicit device_id."""
    monkeypatch.delenv("MCUBRIDGE_GATEWAY_HOST", raising=False)
    monkeypatch.delenv("MCUBRIDGE_GATEWAY_PORT", raising=False)
    monkeypatch.delenv("MCUBRIDGE_CLOUD_HOST", raising=False)
    monkeypatch.delenv("MCUBRIDGE_CLOUD_PORT", raising=False)
    monkeypatch.delenv("MCUBRIDGE_DEVICE_ID", raising=False)

    args = build_bridge_args(host="127.0.0.1", port=8443, device_id="yun-01", topic_prefix="br")
    assert args == {
        "host": "127.0.0.1",
        "port": 8443,
        "device_id": "yun-01",
        "topic_prefix": "br",
    }
    # Explicit device_id is required: missing device_id raises ValueError
    with pytest.raises(ValueError, match="Explicit target device_id is required"):
        build_bridge_args(host="127.0.0.1", port=8443)


@pytest.mark.asyncio
async def test_spi_device_lifecycle_and_transfer() -> None:
    """SpiDevice context manager, properties, begin/end, and transfer."""
    mock_stub = MagicMock(spec=LocalBridgeStub)
    mock_stub.SpiConfigure = AsyncMock(return_value=pb.GenericResponse(status="ok"))

    def _mock_spi_transfer(req: pb.SpiTransfer) -> pb.SpiTransferResponse:
        return pb.SpiTransferResponse(data=req.data)

    mock_stub.SpiTransfer = AsyncMock(side_effect=_mock_spi_transfer)

    dev = SpiDevice(mock_stub, frequency=2000000, bit_order=SpiBitOrder.LSBFIRST, mode=SpiMode.MODE1)
    assert dev.frequency == 2000000
    assert dev.bit_order == SpiBitOrder.LSBFIRST
    assert dev.mode == SpiMode.MODE1

    async with dev as spi:
        assert spi is dev
        resp = await dev.transfer(b"\x01\x02\x03")
        assert resp == b"\x01\x02\x03"

    mock_stub.SpiConfigure.assert_awaited()
    mock_stub.SpiTransfer.assert_awaited()


def test_topic_build_and_match() -> None:
    """Topic construction and matching logic."""
    assert Topic.build("mcu", "status") == f"{DEFAULT_TOPIC_PREFIX}/mcu/status"
    assert Topic.build("mcu", "status", prefix="custom") == "custom/mcu/status"


def test_topic_matches_wildcards() -> None:
    assert Topic.matches("br/#", "br/a/1")
    assert Topic.matches("br/a/1", "br/a/1")
    assert not Topic.matches("br/a/1", "br/a/2")


@pytest.mark.asyncio
async def test_smoke_connection_run_test(mocker: MockerFixture) -> None:
    """Verify test_smoke_connection.run_test calls bridge_session correctly."""
    import test_smoke_connection

    mock_sess = mocker.patch("test_smoke_connection.bridge_session")
    mock_chan = MagicMock()
    mock_stub = MagicMock()
    mock_sess.return_value.__aenter__.return_value = (mock_chan, mock_stub)
    await test_smoke_connection.run_test(host="127.0.0.1", port=8443, device_id="yun-01", topic_prefix="br")
    mock_sess.assert_called_once_with(host="127.0.0.1", port=8443, device_id="yun-01", topic_prefix="br")


def test_smoke_connection_cli_invocation(mocker: MockerFixture) -> None:
    """Verify test_smoke_connection CLI entry point invokes run_test via typer runner."""
    import test_smoke_connection

    mock_run = mocker.patch("test_smoke_connection.run_test")
    runner = CliRunner()
    res = runner.invoke(
        cast(Any, test_smoke_connection.cli),
        ["--host", "127.0.0.1", "--port", "8443", "--device-id", "yun-01", "--topic-prefix", "test"],
    )
    assert res.exit_code == 0
    mock_run.assert_called_once_with("127.0.0.1", 8443, "yun-01", "test")
