import importlib
from pytest_mock import MockerFixture


def test_daemon_app_invokes_entrypoint(mocker: MockerFixture) -> None:
    module = importlib.import_module("mcubridge.daemon")
    called = mocker.patch.object(module, "app")

    module.app()

    called.assert_called_once_with()
