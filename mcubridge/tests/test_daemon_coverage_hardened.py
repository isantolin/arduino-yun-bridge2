"""SIL-2 Daemon Coverage Hardening Test Suite.

Genuinely exercises daemon execution lifecycle, crypto verification failure,
strict mode default secret behavior, signal/exception handling, and CLI entry points.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import mcubridge.daemon as daemon
from mcubridge.config.const import DEFAULT_SERIAL_SHARED_SECRET
from mcubridge.config.settings import RuntimeConfig


def test_daemon_crypto_verification_failure() -> None:
    with (
        patch("mcubridge.daemon.load_runtime_config") as mock_cfg,
        patch("mcubridge.daemon.configure_logging"),
        patch("mcubridge.daemon.verify_crypto_integrity", return_value=False),
        pytest.raises(SystemExit) as exc_info,
    ):
        mock_cfg.return_value = RuntimeConfig(
            topic_prefix="test/br",
            serial_port="/dev/null",
            serial_baud=115200,
        )
        daemon.run_daemon()
    assert exc_info.value.code == 1


def test_daemon_strict_mode_and_keyboard_interrupt() -> None:
    config = RuntimeConfig(
        topic_prefix="test/br",
        serial_port="/dev/null",
        serial_baud=115200,
        serial_shared_secret=DEFAULT_SERIAL_SHARED_SECRET,
        cloud_enabled=True,
    )

    def run_side_effect(coro: Any) -> None:
        coro.close()
        raise KeyboardInterrupt

    with (
        patch("mcubridge.daemon.load_runtime_config", return_value=config),
        patch("mcubridge.daemon.configure_logging"),
        patch("mcubridge.daemon.verify_crypto_integrity", return_value=True),
        patch("mcubridge.daemon.asyncio.Runner") as mock_runner,
    ):
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.run.side_effect = run_side_effect
        mock_runner.return_value = mock_instance

        # Must cleanly catch KeyboardInterrupt without exiting 1
        daemon.run_daemon()
        assert not config.cloud_enabled


def test_daemon_fatal_exception_exit() -> None:
    config = RuntimeConfig(
        topic_prefix="test/br",
        serial_port="/dev/null",
        serial_baud=115200,
        serial_shared_secret=b"custom_secret_12345",
    )

    def run_side_effect(coro: Any) -> None:
        coro.close()
        raise OSError("Serial port vanished")

    with (
        patch("mcubridge.daemon.load_runtime_config", return_value=config),
        patch("mcubridge.daemon.configure_logging"),
        patch("mcubridge.daemon.verify_crypto_integrity", return_value=True),
        patch("mcubridge.daemon.asyncio.Runner") as mock_runner,
        pytest.raises(SystemExit) as exc_info,
    ):
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.run.side_effect = run_side_effect
        mock_runner.return_value = mock_instance

        daemon.run_daemon()
    assert exc_info.value.code == 1


def test_daemon_exception_group_exit() -> None:
    config = RuntimeConfig(
        topic_prefix="test/br",
        serial_port="/dev/null",
        serial_baud=115200,
        serial_shared_secret=b"custom_secret_12345",
    )

    def run_side_effect(coro: Any) -> None:
        coro.close()
        raise ExceptionGroup(
            "TaskGroup failure",
            [OSError("Serial write failure"), RuntimeError("Buffer overflow")],
        )

    with (
        patch("mcubridge.daemon.load_runtime_config", return_value=config),
        patch("mcubridge.daemon.configure_logging"),
        patch("mcubridge.daemon.verify_crypto_integrity", return_value=True),
        patch("mcubridge.daemon.asyncio.Runner") as mock_runner,
        pytest.raises(SystemExit) as exc_info,
    ):
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.run.side_effect = run_side_effect
        mock_runner.return_value = mock_instance

        daemon.run_daemon()
    assert exc_info.value.code == 1


def test_daemon_main_and_app_cli() -> None:
    with patch("mcubridge.daemon.run_daemon") as mock_run:
        daemon.main()
        assert mock_run.called

        mock_run.reset_mock()
        daemon.app()
        assert mock_run.called
