"""Test suite for mcubridge daemon lifecycle, error recovery, and CLI. [SIL-2]"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.daemon import app as daemon_app
from mcubridge.daemon import cli as daemon_cli
from mcubridge.daemon import main as daemon_main
from mcubridge.daemon import run_daemon
from mcubridge.protocol.protocol import DEFAULT_SERIAL_SHARED_SECRET
from pytest_mock import MockerFixture
from typer.testing import CliRunner


def test_daemon_cli_help() -> None:
    """Validate daemon CLI help documentation and execution."""
    runner = CliRunner()
    res = runner.invoke(daemon_cli, ["--help"])
    assert res.exit_code == 0
    assert "Arduino MCU Bridge" in res.output or "daemon" in res.output.lower()

    with pytest.raises(SystemExit) as exc_info:
        daemon_app(["--help"])
    assert exc_info.value.code == 0


def test_daemon_entrypoints_call_run_daemon(mocker: MockerFixture) -> None:
    """Validate app() and main() dispatch cleanly to run_daemon()."""
    mock_run = mocker.patch("mcubridge.daemon.run_daemon")
    daemon_main()
    assert mock_run.call_count == 1

    daemon_app()
    assert mock_run.call_count == 2


def test_daemon_crypto_verification_failure(mocker: MockerFixture) -> None:
    """Validate fail-fast abort when cryptographic Known-Answer Tests fail."""
    mocker.patch("mcubridge.daemon.verify_crypto_integrity", return_value=False)
    mocker.patch("mcubridge.daemon.load_runtime_config")
    mocker.patch("mcubridge.daemon.configure_logging")

    with pytest.raises(SystemExit) as exc:
        run_daemon()
    assert exc.value.code == 1


def test_daemon_strict_mode_when_default_secret(mocker: MockerFixture) -> None:
    """Validate that cloud transport is forcibly disabled when default secret is used."""
    config = RuntimeConfig(
        topic_prefix="test/br",
        serial_port="/dev/null",
        serial_shared_secret=DEFAULT_SERIAL_SHARED_SECRET,
        cloud_enabled=True,
    )
    mocker.patch("mcubridge.daemon.verify_crypto_integrity", return_value=True)
    mocker.patch("mcubridge.daemon.load_runtime_config", return_value=config)
    mocker.patch("mcubridge.daemon.configure_logging")
    mock_runner = MagicMock()
    mock_runner.__enter__.return_value = mock_runner

    def _close_coro(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()

    mock_runner.run.side_effect = _close_coro
    mocker.patch("asyncio.Runner", return_value=mock_runner)

    run_daemon()
    assert config.cloud_enabled is False


def test_daemon_keyboard_interrupt_graceful_exit(mocker: MockerFixture) -> None:
    """Validate graceful handling and logging of KeyboardInterrupt."""
    mocker.patch("mcubridge.daemon.verify_crypto_integrity", return_value=True)
    mocker.patch("mcubridge.daemon.load_runtime_config", side_effect=KeyboardInterrupt)
    mock_log = mocker.patch("mcubridge.daemon.logger.info")

    run_daemon()
    assert mock_log.called


def test_daemon_fatal_exception_exit(mocker: MockerFixture) -> None:
    """Validate that fatal exceptions cause exit(1) with cleanup."""
    mock_cfg = MagicMock()
    mock_cfg.serial_shared_secret = b"test_secret"
    mocker.patch("mcubridge.daemon.load_runtime_config", return_value=mock_cfg)
    mocker.patch("mcubridge.daemon.configure_logging")
    mocker.patch("mcubridge.daemon.verify_crypto_integrity", return_value=True)
    mock_state = MagicMock()
    mocker.patch("mcubridge.daemon.create_runtime_state", return_value=mock_state)
    mocker.patch("mcubridge.daemon.SerialTransport")
    mocker.patch("mcubridge.daemon.BridgeService", side_effect=ValueError("Service init fail"))

    with pytest.raises(SystemExit) as exc:
        run_daemon()
    assert exc.value.code == 1
    mock_state.cleanup.assert_called_once()


def test_daemon_exception_group_handled_exit(mocker: MockerFixture) -> None:
    """Validate handled ExceptionGroup branches result in clean exit(1)."""
    mocker.patch("mcubridge.daemon.verify_crypto_integrity", return_value=True)
    exc_group = ExceptionGroup("group", [OSError("Serial disconnected"), ValueError("Parse error")])
    mocker.patch("mcubridge.daemon.load_runtime_config", side_effect=exc_group)

    with pytest.raises(SystemExit) as exc:
        run_daemon()
    assert exc.value.code == 1


def test_daemon_exception_group_unhandled_raises(mocker: MockerFixture) -> None:
    """Validate unhandled exceptions inside ExceptionGroup are re-raised."""
    mocker.patch("mcubridge.daemon.verify_crypto_integrity", return_value=True)
    exc_group = ExceptionGroup("group", [KeyError("unhandled")])
    mocker.patch("mcubridge.daemon.load_runtime_config", side_effect=exc_group)

    with pytest.raises(ExceptionGroup):
        run_daemon()


def test_daemon_exception_group_partially_unhandled_raises(mocker: MockerFixture) -> None:
    """Validate mixed ExceptionGroup with unhandled exception raises the unhandled sub-group."""
    mock_cfg = MagicMock()
    mock_cfg.serial_shared_secret = b""
    mocker.patch("mcubridge.daemon.load_runtime_config", return_value=mock_cfg)
    mocker.patch("mcubridge.daemon.configure_logging")
    mocker.patch("mcubridge.daemon.verify_crypto_integrity", return_value=True)
    mocker.patch("mcubridge.daemon.create_runtime_state", return_value=MagicMock())
    mocker.patch("mcubridge.daemon.SerialTransport")
    mocker.patch("mcubridge.daemon.BridgeService", return_value=MagicMock())

    mock_runner = MagicMock()
    mock_runner.__enter__.return_value = mock_runner

    def _raise_mixed(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()
        raise ExceptionGroup("mixed", [OSError("err"), KeyError("unhandled")])

    mock_runner.run.side_effect = _raise_mixed
    mocker.patch("asyncio.Runner", return_value=mock_runner)

    with pytest.raises(ExceptionGroup):
        run_daemon()
