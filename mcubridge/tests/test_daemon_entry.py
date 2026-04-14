import importlib
from typing import Any
from unittest.mock import MagicMock


def test_daemon_main_invokes_entrypoint(monkeypatch: Any):
    module = importlib.import_module("mcubridge.daemon")
    called = MagicMock()
    monkeypatch.setattr(module, "main", called)

    module.main()

    called.assert_called_once_with()
