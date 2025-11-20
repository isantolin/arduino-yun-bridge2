import asyncio

import pytest

from yunbridge.mqtt import client as client_module


def _make_client(loop: asyncio.AbstractEventLoop):
    instance = client_module.Client.__new__(client_module.Client)
    connected = loop.create_future()
    disconnected = loop.create_future()
    object.__setattr__(instance, "_connected", connected)
    object.__setattr__(instance, "_disconnected", disconnected)
    return instance, connected, disconnected


def test_disconnect_cancels_pending_future(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.new_event_loop()
    try:
        instance, connected, disconnected = _make_client(loop)
        connected.cancel()

        base_called = False

        def fake_base(self, client, userdata, *args, **kwargs):
            nonlocal base_called
            base_called = True

        monkeypatch.setattr(
            client_module.BaseClient,
            "_on_disconnect",
            fake_base,
            raising=False,
        )

        client_module.Client._on_disconnect(instance, object(), object())

        assert disconnected.cancelled()
        assert base_called is False
    finally:
        loop.close()


def test_disconnect_swallows_cancelled_error(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.new_event_loop()
    try:
        instance, connected, disconnected = _make_client(loop)

        def fake_base(self, client, userdata, *args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(
            client_module.BaseClient,
            "_on_disconnect",
            fake_base,
            raising=False,
        )

        client_module.Client._on_disconnect(instance, object(), object())

        assert disconnected.cancelled()
        assert connected.cancelled() is False
    finally:
        loop.close()


def test_disconnect_delegates_to_base(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.new_event_loop()
    try:
        instance, connected, disconnected = _make_client(loop)
        disconnected.set_result(None)

        base_called = False

        def fake_base(self, client, userdata, *args, **kwargs):
            nonlocal base_called
            base_called = True

        monkeypatch.setattr(
            client_module.BaseClient,
            "_on_disconnect",
            fake_base,
            raising=False,
        )

        client_module.Client._on_disconnect(instance, "client", "userdata")

        assert base_called is True
        assert disconnected.cancelled() is False
        assert connected.cancelled() is False
    finally:
        loop.close()
