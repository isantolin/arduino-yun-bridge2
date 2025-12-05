"""Tests for daemon entry points and TLS helpers."""
from __future__ import annotations

import asyncio
from attrs import evolve
from pathlib import Path
from typing import Any, cast

import pytest

import yunbridge.daemon as daemon
import yunbridge.config.tls as tls_module
from yunbridge.const import MQTT_TLS_MIN_VERSION


def test_main_async_runs_all_tasks(
    monkeypatch: pytest.MonkeyPatch,
    runtime_config,
) -> None:
    calls: list[str] = []

    async def _stub_serial(*_: Any) -> None:
        calls.append("serial")

    async def _stub_mqtt(*_: Any) -> None:
        calls.append("mqtt")

    async def _stub_status(*_: Any) -> None:
        calls.append("status")

    async def _stub_metrics(*_: Any) -> None:
        calls.append("metrics")

    cleanup_called: list[bool] = []

    monkeypatch.setattr(daemon, "serial_reader_task", _stub_serial)
    monkeypatch.setattr(daemon, "mqtt_task", _stub_mqtt)
    monkeypatch.setattr(daemon, "status_writer", _stub_status)
    monkeypatch.setattr(daemon, "publish_metrics", _stub_metrics)
    monkeypatch.setattr(daemon, "_build_mqtt_tls_context", lambda _cfg: None)
    monkeypatch.setattr(
        daemon,
        "cleanup_status_file",
        lambda: cleanup_called.append(True),
    )

    asyncio.run(daemon.main_async(runtime_config))

    assert calls == ["serial", "mqtt", "status", "metrics"]
    assert cleanup_called


def test_main_async_tls_error(
    monkeypatch: pytest.MonkeyPatch,
    runtime_config,
) -> None:
    def _raise_tls(_cfg):
        raise RuntimeError("tls")

    monkeypatch.setattr(daemon, "_build_mqtt_tls_context", _raise_tls)

    with pytest.raises(RuntimeError, match="TLS configuration invalid"):
        asyncio.run(daemon.main_async(runtime_config))


def test_build_mqtt_tls_context_with_client_cert(
    monkeypatch: pytest.MonkeyPatch,
    runtime_config,
    tmp_path: Path,
) -> None:
    cafile = tmp_path / "ca.pem"
    cafile.write_text("dummy")
    certfile = tmp_path / "cert.pem"
    certfile.write_text("cert")
    keyfile = tmp_path / "key.pem"
    keyfile.write_text("key")

    class _DummyContext:
        def __init__(self) -> None:
            self.minimum_version = None
            self.loaded: tuple[str, str] | None = None

        def load_cert_chain(self, certfile: str, keyfile: str) -> None:
            self.loaded = (certfile, keyfile)

    dummy_context = _DummyContext()

    def _fake_context(
        purpose,
        *,
        cafile: str | None = None,
        **_: Any,
    ) -> _DummyContext:
        assert cafile == str(cafile_path)
        return dummy_context

    cafile_path = cafile
    monkeypatch.setattr(
        tls_module.ssl, "create_default_context", _fake_context
    )

    cfg = evolve(
        runtime_config,
        mqtt_cafile=str(cafile_path),
        mqtt_certfile=str(certfile),
        mqtt_keyfile=str(keyfile),
    )

    context = daemon._build_mqtt_tls_context(cfg)

    assert context is dummy_context
    assert cast(Any, context).minimum_version == MQTT_TLS_MIN_VERSION
    assert cast(Any, context).loaded == (str(certfile), str(keyfile))


def test_build_mqtt_tls_context_missing_cafile(
    runtime_config,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.pem"
    cfg = evolve(runtime_config, mqtt_cafile=str(missing))

    with pytest.raises(RuntimeError, match="TLS CA file does not exist"):
        daemon._build_mqtt_tls_context(cfg)


def test_main_exits_on_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    runtime_config,
) -> None:
    monkeypatch.setattr(daemon, "load_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(daemon, "configure_logging", lambda _cfg: None)

    def _raise_runtime(coro):
        coro.close()
        raise RuntimeError("boom")

    monkeypatch.setattr(daemon.asyncio, "run", _raise_runtime)

    exit_codes: list[int] = []

    def _fake_exit(code: int) -> None:
        exit_codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(daemon.sys, "exit", _fake_exit)

    with pytest.raises(SystemExit):
        daemon.main()

    assert exit_codes == [1]
