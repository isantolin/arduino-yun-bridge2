from typing import Any
import importlib
from unittest.mock import MagicMock


def test_daemon_app_invokes_entrypoint(monkeypatch: Any):
    module = importlib.import_module("mcubridge.daemon")
    called = MagicMock()
    monkeypatch.setattr(module, "app", called)

    module.app()

    called.assert_called_once_with()
