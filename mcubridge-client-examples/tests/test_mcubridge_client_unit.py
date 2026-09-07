"""Comprehensive unit tests for mcubridge_client (cli, env, spi, definitions). [SIL-2]"""

from __future__ import annotations

from typing import Any, cast

import structlog
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from grpclib.client import Channel
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
            mock_chan = MagicMock(spec=Channel)
            mock_stub = MagicMock(spec=LocalBridgeStub)
            mock_chan_cls.return_value = mock_chan
            mock_stub_cls.return_value = mock_stub

            async with bridge_session("/tmp/test.sock", "br") as (chan, stub):
                assert chan is mock_chan
                assert stub is mock_stub

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
                mock_mod.get_uci_config = MagicMock(return_value={"socket_path": "/var/run/test.sock", "_private": "x"})
                mock_imp.return_value = mock_mod
                res = read_uci_general()
                assert res == {"socket_path": "/var/run/test.sock"}

                # Exception path in get_uci_config
                mock_mod.get_uci_config.side_effect = RuntimeError("UCI error")
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
    assert "socket_path=" in captured.out


# ==============================================================================
# spi.py & definitions.py tests
# ==============================================================================


def test_definitions_build_bridge_args() -> None:
    """build_bridge_args builds dictionary from parameters."""
    assert build_bridge_args() == {
        "socket_path": "/var/run/mcubridge.sock",
        "topic_prefix": "br",
    }
    assert build_bridge_args("/tmp/sock", "test_prefix") == {
        "socket_path": "/tmp/sock",
        "topic_prefix": "test_prefix",
    }


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
        await test_smoke_connection.run_test("/tmp/fake.sock", "br")
        mock_sess.assert_called_once_with("/tmp/fake.sock", "br")


def test_smoke_connection_cli_invocation() -> None:
    """Verify test_smoke_connection CLI entry point invokes run_test via typer runner."""
    import test_smoke_connection

    with patch("test_smoke_connection.bridge_session") as mock_sess:
        mock_chan = MagicMock()
        mock_stub = MagicMock()
        mock_sess.return_value.__aenter__.return_value = (mock_chan, mock_stub)
        runner = CliRunner()
        res = runner.invoke(
            cast(Any, test_smoke_connection.cli), ["--socket-path", "/tmp/fake.sock", "--topic-prefix", "test"]
        )
        assert res.exit_code == 0
        mock_sess.assert_called_once_with("/tmp/fake.sock", "test")
