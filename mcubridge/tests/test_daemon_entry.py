import importlib
from unittest.mock import MagicMock

import pytest
from mcubridge.daemon import app as daemon_app
from mcubridge.daemon import cli as daemon_cli
from mcubridge.daemon import run_daemon
from pytest_mock import MockerFixture
from typer.testing import CliRunner


def test_daemon_app_invokes_entrypoint(mocker: MockerFixture) -> None:
    module = importlib.import_module("mcubridge.daemon")
    called = mocker.patch.object(module, "app")

    module.app()

    called.assert_called_once_with()


def test_daemon_app_version() -> None:
    runner = CliRunner()
    res = runner.invoke(daemon_cli, ["--help"])
    assert res.exit_code == 0
    assert "Arduino MCU Bridge" in res.output or "daemon" in res.output.lower()

    with pytest.raises(SystemExit) as exc_info:
        daemon_app(["--help"])
    assert exc_info.value.code == 0


def test_daemon_app_calls_run_daemon(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("mcubridge.daemon.run_daemon")
    daemon_app()
    mock_run.assert_called_once_with()


def test_daemon_run_branches(mocker: MockerFixture) -> None:
    mock_cfg = MagicMock()
    mock_cfg.serial_shared_secret = b""
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

    mocker.patch("mcubridge.daemon.BridgeService", return_value=MagicMock())
    mock_runner = MagicMock()
    mock_runner.__enter__.return_value = mock_runner
    mock_runner.run.side_effect = ExceptionGroup("mixed", [OSError("err"), KeyError("unhandled")])
    mocker.patch("asyncio.Runner", return_value=mock_runner)

    with pytest.raises(ExceptionGroup):
        run_daemon()


def test_daemon_main_function(mocker: MockerFixture) -> None:
    from mcubridge.daemon import main

    mock_run = mocker.patch("mcubridge.daemon.run_daemon")
    main()
    mock_run.assert_called_once_with()
