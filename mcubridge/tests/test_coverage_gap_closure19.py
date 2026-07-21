"""Exhaustive gap closure suite 19 for Python daemon SIL-2 coverage (95%+ target)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import load_runtime_config
from mcubridge.daemon import app
from mcubridge_client.protocol import Topic
from mcubridge_client.spi import SpiDevice


@pytest.mark.asyncio
async def test_protocol_topic_matches_and_spi_device_auto_begin():
    # 1. Topic.matches
    assert Topic.matches("a/+/c", "a/b/c") is True
    assert Topic.matches("a/#", "a/b/c/d") is True
    assert Topic.matches("x/y", "a/b") is False

    # 2. SpiDevice.transfer when self._active is False (auto begin)
    mock_bridge = AsyncMock()
    mock_bridge.spi_transfer.return_value = b"\x01\x02"
    spi = SpiDevice(mock_bridge)
    assert spi._active is False  # type: ignore[reportPrivateUsage]

    res = await spi.transfer(b"\x00\x00")
    assert res == b"\x01\x02"
    assert spi._active is True  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_daemon_config_and_exception_group_branches():
    cfg = load_runtime_config()
    cfg.serial_shared_secret = b""

    def dummy_run(coro: Any) -> None:
        coro.close()

    # 1. daemon app when serial_shared_secret is empty
    with (
        patch("sys.argv", ["mcubridge"]),
        patch("mcubridge.daemon.load_runtime_config", return_value=cfg),
        patch("mcubridge.daemon.asyncio.Runner") as mock_runner,
    ):
        mock_runner_inst = MagicMock()
        mock_runner_inst.run.side_effect = dummy_run
        mock_runner.return_value.__enter__.return_value = mock_runner_inst
        app()
        mock_runner_inst.run.assert_called_once()

    # 2. daemon app with unhandled ExceptionGroup exception
    eg = ExceptionGroup("Group", [OSError("Handled"), KeyError("Unhandled")])
    with patch("sys.argv", ["mcubridge"]), patch("mcubridge.daemon.load_runtime_config", side_effect=eg):
        with pytest.raises((KeyError, ExceptionGroup)):
            app()


@pytest.mark.asyncio
async def test_logging_syslog_path_branches():
    cfg = load_runtime_config()

    # 1. configure_logging with /dev/log
    with (
        patch("mcubridge.config.logging.Path.exists", return_value=True),
        patch("mcubridge.config.logging.SysLogHandler"),
    ):
        configure_logging(cfg)

    # 2. configure_logging with /var/run/log (/dev/log returns False, /var/run/log returns True)
    def mock_path_exists(self_obj: Any = None) -> bool:
        path_str = str(self_obj) if self_obj else ""
        return "/var/run/log" in path_str

    with (
        patch("mcubridge.config.logging.Path.exists", side_effect=mock_path_exists),
        patch("mcubridge.config.logging.SysLogHandler"),
    ):
        configure_logging(cfg)
