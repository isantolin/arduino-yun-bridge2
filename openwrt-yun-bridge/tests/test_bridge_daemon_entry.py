from unittest.mock import MagicMock

import importlib


def test_bridge_daemon_main_invokes_daemon(monkeypatch):
    module = importlib.import_module("bridge_daemon")
    called = MagicMock()
    monkeypatch.setattr(module, "_main", called)

    module.main()

    called.assert_called_once_with()
