"""Regression tests for the pin_rest_cgi MQTT helper."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, cast

import pytest


def _load_pin_rest_cgi() -> ModuleType:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "openwrt-yun-core"
        / "scripts"
        / "pin_rest_cgi.py"
    )
    spec = importlib.util.spec_from_file_location("pin_rest_cgi", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load pin_rest_cgi script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[misc]
    return module


@pytest.fixture()
def pin_rest_module() -> ModuleType:
    module = _load_pin_rest_cgi()
    # Isolate retries to make tests deterministic
    typed_module = cast(Any, module)
    typed_module.DEFAULT_RETRIES = 3
    typed_module.DEFAULT_PUBLISH_TIMEOUT = 0.5
    typed_module.DEFAULT_BACKOFF_BASE = 0.01
    return module


class _FlakyClient:
    attempts: int = 0
    fail_until: int = 2

    def __init__(self) -> None:
        self._connected = False
        self._loop_started = False
        self._published = False

    def connect(self, *_: Any, **__: Any) -> None:
        type(self).attempts += 1
        if type(self).attempts <= self.fail_until:
            raise OSError("simulated connect failure")
        self._connected = True

    def loop_start(self) -> None:
        self._loop_started = True

    def publish(self, *_: Any, **__: Any) -> "_Result":
        self._published = True
        return _Result()

    def loop_stop(self) -> None:
        self._loop_started = False

    def disconnect(self) -> None:
        self._connected = False


class _Result:
    def __init__(self) -> None:
        self._published = True

    def is_published(self) -> bool:
        return self._published


class _HangingResult(_Result):
    def __init__(self) -> None:
        super().__init__()
        self._published = False

    def is_published(self) -> bool:
        return False


class _HangingClient(_FlakyClient):
    def connect(self, *_: Any, **__: Any) -> None:
        self._connected = True

    def publish(self, *_: Any, **__: Any) -> _HangingResult:
        return _HangingResult()


def _no_sleep(_: float) -> None:
    return None


def test_publish_succeeds_after_retries(
    pin_rest_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FlakyClient.attempts = 0
    monkeypatch.setattr(pin_rest_module.mqtt, "Client", lambda: _FlakyClient())
    sleep_calls: list[float] = []

    def _sleep(delay: float) -> None:
        sleep_calls.append(delay)

    pin_rest_module.publish_with_retries(
        "br/test",
        "1",
        retries=3,
        publish_timeout=0.5,
        base_delay=0.01,
        sleep_fn=_sleep,
    )

    assert _FlakyClient.attempts == 3
    assert sleep_calls  # ensures backoff executed at least once


def test_publish_raises_after_timeout(
    pin_rest_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FlakyClient.attempts = 0
    monkeypatch.setattr(
        pin_rest_module.mqtt, "Client", lambda: _HangingClient()
    )

    with pytest.raises(TimeoutError):
        pin_rest_module.publish_with_retries(
            "br/test",
            "1",
            retries=1,
            publish_timeout=0.0,
            base_delay=0,
            sleep_fn=_no_sleep,
        )


def test_publish_propagates_last_error(
    pin_rest_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FlakyClient.attempts = 0
    errors: Dict[str, int] = {"count": 0}

    class _AlwaysFailClient(_FlakyClient):
        def connect(self, *_: Any, **__: Any) -> None:
            errors["count"] += 1
            raise RuntimeError("boom")

    monkeypatch.setattr(
        pin_rest_module.mqtt, "Client", lambda: _AlwaysFailClient()
    )

    with pytest.raises(RuntimeError, match="boom"):
        pin_rest_module.publish_with_retries(
            "br/test",
            "payload",
            retries=2,
            publish_timeout=0.1,
            base_delay=0.0,
            sleep_fn=_no_sleep,
        )

    assert errors["count"] == 2
