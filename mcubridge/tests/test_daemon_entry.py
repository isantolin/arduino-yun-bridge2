import importlib

from pytest_mock import MockerFixture


def test_daemon_app_invokes_entrypoint(mocker: MockerFixture) -> None:
    module = importlib.import_module("mcubridge.daemon")
    called = mocker.patch.object(module, "app")

    module.app()

    called.assert_called_once_with()


def test_daemon_app_version() -> None:
    import pytest
    from mcubridge.daemon import app as daemon_app
    from mcubridge.daemon import cli as daemon_cli
    from typer.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(daemon_cli, ["--help"])
    assert res.exit_code == 0
    assert "Arduino MCU Bridge" in res.output or "daemon" in res.output.lower()

    with pytest.raises(SystemExit) as exc_info:
        daemon_app(["--help"])
    assert exc_info.value.code == 0
