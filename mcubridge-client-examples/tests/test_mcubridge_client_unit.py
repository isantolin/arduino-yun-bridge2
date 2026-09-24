"""Comprehensive unit tests for mcubridge_client (cli, env, spi, definitions). [SIL-2]"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from typer.testing import CliRunner

from mcubridge_client import (
    LocalBridgeStub,
    SpiBitOrder,
    SpiDevice,
    SpiMode,
    build_bridge_args,
    dump_client_env,
    pb,
)
from mcubridge_client.cli import bridge_session, configure_logging
from mcubridge_client.env import is_openwrt, read_uci_general

# ==============================================================================
# cli.py & env.py tests
# ==============================================================================


def test_cli_configure_logging() -> None:
    """configure_logging sets up basic logging without raising exceptions."""
    configure_logging()
    structlog.get_logger("test").info("logging configured")
    assert structlog.is_configured()


@pytest.mark.asyncio
async def test_cli_bridge_session() -> None:
    """bridge_session context manager yields Channel and LocalBridgeStub."""
    with patch("mcubridge_client.cli.Channel") as mock_chan_cls:
        with patch("mcubridge_client.cli.LocalBridgeStub") as mock_stub_cls:
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


def test_env_is_openwrt() -> None:
    """is_openwrt checks environment variable and file presence."""
    with patch.dict("os.environ", {"MCUBRIDGE_FORCE_UCI": "1"}):
        assert is_openwrt() is True

    with patch.dict("os.environ", {}, clear=True):
        with patch("pathlib.Path.exists", return_value=True):
            assert is_openwrt() is True


def test_env_read_uci_general() -> None:
    """read_uci_general returns UCI config dict or empty dict."""
    with patch("mcubridge_client.env.is_openwrt", return_value=False):
        assert read_uci_general() == {}

    with patch("mcubridge_client.env.is_openwrt", return_value=True):
        with patch("importlib.util.find_spec", return_value=MagicMock()):
            with patch("importlib.import_module") as mock_imp:
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
    """dump_client_env outputs snapshot to logger or stdout."""
    # 1. Custom logger
    mock_log = MagicMock()
    dump_client_env(mock_log)
    assert mock_log.info.call_count >= 2

    # 2. Stdout fallback
    dump_client_env(None)
    captured = capsys.readouterr()
    assert "gateway_host=" in captured.out
    assert "target_device_id=" in captured.out


# ==============================================================================
# spi.py & definitions.py tests
# ==============================================================================


def test_definitions_build_bridge_args() -> None:
    """build_bridge_args builds dictionary targeting Gateway with explicit device_id."""
    with patch.dict("os.environ", {}, clear=True):
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

    async with dev as active_dev:
        assert active_dev is dev
        mock_stub.SpiConfigure.assert_called_once()

        # Verify protobuf SpiConfig payload was sent accurately
        cfg_pb = mock_stub.SpiConfigure.call_args[0][0]
        assert cfg_pb.frequency == 2000000
        assert cfg_pb.bit_order == SpiBitOrder.LSBFIRST.value
        assert cfg_pb.data_mode == SpiMode.MODE1.value

        # Idempotent begin
        await dev.begin()

        # Transfer with bytes and Sequence[int]
        res1 = await dev.transfer(b"\x01\x02")
        assert res1 == b"\x01\x02"

        res2 = await dev.transfer([1, 2, 3])
        assert res2 == b"\x01\x02\x03"

    await dev.end()


def test_topic_matches_wildcards() -> None:
    """Verify Topic.matches works for exact and wildcard patterns."""
    from mcubridge_client.protocol import Topic

    assert Topic.matches("br/+/status", "br/system/status")
    assert Topic.matches("br/#", "br/a/1")
    assert Topic.matches("br/a/1", "br/a/1")
    assert not Topic.matches("br/a/1", "br/a/2")


@pytest.mark.asyncio
async def test_smoke_connection_run_test() -> None:
    """Verify test_smoke_connection.run_test calls bridge_session correctly."""
    import test_smoke_connection

    with patch("test_smoke_connection.bridge_session") as mock_sess:
        mock_chan = MagicMock()
        mock_stub = MagicMock()
        mock_sess.return_value.__aenter__.return_value = (mock_chan, mock_stub)
        await test_smoke_connection.run_test(host="127.0.0.1", port=8443, device_id="yun-01", topic_prefix="br")
        mock_sess.assert_called_once_with(host="127.0.0.1", port=8443, device_id="yun-01", topic_prefix="br")


def test_smoke_connection_cli_invocation() -> None:
    """Verify test_smoke_connection CLI entry point invokes run_test via typer runner."""
    import test_smoke_connection

    with patch("test_smoke_connection.run_test") as mock_run:
        runner = CliRunner()
        res = runner.invoke(
            cast(Any, test_smoke_connection.cli),
            ["--host", "127.0.0.1", "--port", "8443", "--device-id", "yun-01", "--topic-prefix", "test"],
        )
        assert res.exit_code == 0
        mock_run.assert_called_once_with("127.0.0.1", 8443, "yun-01", "test")


@pytest.mark.asyncio
async def test_gateway_northbound_run_test() -> None:
    """Verify test_gateway_northbound.run_test calls DispatchCommand correctly."""
    import test_gateway_northbound

    with patch("test_gateway_northbound.Channel") as mock_chan_cls, \
         patch("test_gateway_northbound.mcubridge_grpc.CloudBridgeStub") as mock_stub_cls:
        mock_chan = MagicMock()
        mock_chan_cls.return_value = mock_chan
        mock_stub = MagicMock()
        mock_stub_cls.return_value = mock_stub
        mock_stub.DispatchCommand = AsyncMock(return_value=MagicMock(status_code=200, payload=b"OK"))

        await test_gateway_northbound.run_test(host="127.0.0.1", port=8443, device_id="yun-01")
        mock_stub.DispatchCommand.assert_awaited_once()


def test_gateway_northbound_cli_invocation() -> None:
    """Verify test_gateway_northbound CLI entry point invokes run_test via typer runner."""
    import test_gateway_northbound

    with patch("test_gateway_northbound.run_test") as mock_run:
        runner = CliRunner()
        res = runner.invoke(
            cast(Any, test_gateway_northbound.cli),
            ["--host", "127.0.0.1", "--port", "8443", "--device-id", "yun-01"],
        )
        assert res.exit_code == 0
        mock_run.assert_called_once_with("127.0.0.1", 8443, "yun-01")


def test_gateway_northbound_cli_missing_device() -> None:
    """Verify test_gateway_northbound CLI raises error when device_id is omitted."""
    import test_gateway_northbound

    runner = CliRunner()
    res = runner.invoke(
        cast(Any, test_gateway_northbound.cli),
        ["--host", "127.0.0.1", "--port", "8443"],
        env={"MCUBRIDGE_DEVICE_ID": ""},
    )
    assert res.exit_code != 0
