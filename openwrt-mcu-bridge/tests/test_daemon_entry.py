from unittest.mock import MagicMock

import importlib


def test_daemon_main_invokes_entrypoint(monkeypatch):
    module = importlib.import_module("mcubridge.daemon")
    called = MagicMock()
    monkeypatch.setattr(module, "main", called)

    module.main()

    called.assert_called_once_with()
