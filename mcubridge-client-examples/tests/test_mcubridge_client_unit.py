"""Unit tests for mcubridge-client and example test scripts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture
import structlog
from typer.testing import CliRunner

from mcubridge_client import (
    BridgeClient,
    build_bridge_args,
    is_openwrt,
    read_uci_general,
)
from mcubridge_client.cli import app, bridge_session
from mcubridge_client.definitions import topic_matches_wildcards


# ==============================================================================
# CLI and Setup Tests
# ==============================================================================


def test_cli_configure_logging() -> None:
    """configure_logging runs without error."""
    from mcubridge_client.cli import configure_logging

    configure_logging()
    logger = structlog.get_logger("test")
    assert logger is not None


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
        mock_chan.__dispatch__.add_listener.assert_called_once()
        callback = mock_chan.__dispatch__.add_listener.call_args[0][1]
        event = MagicMock()
        event.metadata = {}
        await callback(event)
        assert event.metadata["x-device-id"] == "yun-01"

    mock_chan.close.assert_called_once()


def test_env_is_openwrt(mocker: MockerFixture) -> None:
    """is_openwrt checks environment variable and file presence."""
    mocker.patch.dict("os.environ", {"MCUBRIDGE_FORCE_UCI": "1"})
    assert is_openwrt() is True

    mocker.patch.dict("os.environ", {}, clear=True)
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


def test_env_dump_client_env(capsys: pytest.CaptureFixture[str]) -> None:
    """dump_client_env outputs snapshot to logger or stdout."""
    from mcubridge_client.env import dump_client_env

    dump_client_env()
    captured = capsys.readouterr()
    assert captured.out == ""


# ==============================================================================
# Definitions & Arguments Tests
# ==============================================================================


def test_definitions_build_bridge_args(mocker: MockerFixture) -> None:
    """build_bridge_args builds dictionary targeting Gateway with explicit device_id."""
    mocker.patch.dict("os.environ", {}, clear=True)
    args = build_bridge_args(host="127.0.0.1", port=8443, device_id="yun-01", topic_prefix="br")
    assert args == {
        "host": "127.0.0.1",
        "port": 8443,
        "device_id": "yun-01",
        "topic_prefix": "br",
    }

    mocker.patch.dict(
        "os.environ",
        {
            "MCUBRIDGE_GATEWAY_HOST": "10.0.0.2",
            "MCUBRIDGE_GATEWAY_PORT": "9000",
            "MCUBRIDGE_DEVICE_ID": "yun-env",
            "MCUBRIDGE_TOPIC_PREFIX": "env_prefix",
        },
    )
    args_env = build_bridge_args()
    assert args_env == {
        "host": "10.0.0.2",
        "port": 9000,
        "device_id": "yun-env",
        "topic_prefix": "env_prefix",
    }


def test_bridge_client_initialization() -> None:
    """BridgeClient initializes with provided arguments and properties behave correctly."""
    client = BridgeClient(host="localhost", port=1883, device_id="dev-123", topic_prefix="pfx")
    assert client.host == "localhost"
    assert client.port == 1883
    assert client.device_id == "dev-123"
    assert client.topic_prefix == "pfx"


def test_bridge_client_default_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """BridgeClient defaults fall back to build_bridge_args."""
    monkeypatch.delenv("MCUBRIDGE_GATEWAY_HOST", raising=False)
    monkeypatch.delenv("MCUBRIDGE_GATEWAY_PORT", raising=False)
    monkeypatch.delenv("MCUBRIDGE_DEVICE_ID", raising=False)
    monkeypatch.delenv("MCUBRIDGE_TOPIC_PREFIX", raising=False)
    client = BridgeClient()
    assert client.host == "127.0.0.1"
    assert client.port == 8443
    assert client.device_id == "yun-01"
    assert client.topic_prefix == "br"


def test_bridge_client_not_implemented_methods() -> None:
    """Unimplemented abstract methods raise NotImplementedError."""
    client = BridgeClient()
    with pytest.raises(NotImplementedError):
        client.connect()
    with pytest.raises(NotImplementedError):
        client.disconnect()


def test_topic_matches_wildcards() -> None:
    """topic_matches_wildcards correctly checks exact and MQTT wildcards (+ and #)."""
    assert topic_matches_wildcards("sensor/temp", "sensor/temp") is True
    assert topic_matches_wildcards("sensor/temp", "sensor/humidity") is False

    # Plus wildcard
    assert topic_matches_wildcards("sensor/+/reading", "sensor/temp/reading") is True
    assert topic_matches_wildcards("sensor/+/reading", "sensor/temp/high/reading") is False

    # Hash wildcard
    assert topic_matches_wildcards("sensor/#", "sensor/temp/reading") is True
    assert topic_matches_wildcards("sensor/#", "sensor") is True
    assert topic_matches_wildcards("other/#", "sensor/temp") is False

    # Empty pattern/topic
    assert topic_matches_wildcards("", "") is True
    assert topic_matches_wildcards("a", "") is False


# ==============================================================================
# Example Scripts Coverage
# ==============================================================================


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


def test_smoke_connection_cli_invocation() -> None:
    """Verify test_smoke_connection CLI runs without error."""
    import test_smoke_connection

    runner = CliRunner()
    result = runner.invoke(test_smoke_connection.app, ["--help"])
    assert result.exit_code == 0
    assert "Smoke test connecting to local or remote MCU Bridge" in result.output


def test_client_main_cli_help() -> None:
    """CLI --help returns 0."""
    runner = CliRunner()
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0


@pytest.mark.asyncio
async def test_gateway_northbound_run_test(mocker: MockerFixture) -> None:
    """Verify test_gateway_northbound.run_test calls DispatchCommand correctly."""
    import test_gateway_northbound

    mock_chan_cls = mocker.patch("test_gateway_northbound.Channel")
    mock_stub_cls = mocker.patch("test_gateway_northbound.mcubridge_grpc.CloudBridgeStub")
    mock_chan = MagicMock()
    mock_chan_cls.return_value = mock_chan
    mock_stub = MagicMock()
    mock_stub_cls.return_value = mock_stub
    mock_stub.DispatchCommand = AsyncMock(return_value=MagicMock(status_code=200, payload=b"OK"))

    await test_gateway_northbound.run_test(host="127.0.0.1", port=8443, device_id="yun-01")
    mock_stub.DispatchCommand.assert_awaited_once()


def test_gateway_northbound_cli_invocation() -> None:
    """Verify test_gateway_northbound CLI runs without error."""
    import test_gateway_northbound

    runner = CliRunner()
    result = runner.invoke(test_gateway_northbound.app, ["--help"])
    assert result.exit_code == 0
    assert "End-to-end test verifying Gateway Northbound command dispatching." in result.output
