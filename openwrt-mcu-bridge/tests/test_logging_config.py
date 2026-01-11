"""Tests for structured logging configuration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import mcubridge.config.logging as log_mod


def test_serialise_value_handles_bytes_and_objects() -> None:
    record = logging.LogRecord(
        name="mcubridge.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.custom_bytes = b"caf\xc3\xa9"
    record.custom_obj = object()

    formatter = log_mod.StructuredLogFormatter()
    payload = json.loads(formatter.format(record))

    assert payload["logger"] == "test"
    assert payload["message"] == "hello"
    assert payload["extra"]["custom_bytes"] == "café"
    assert isinstance(payload["extra"]["custom_obj"], str)


def test_build_handler_prefers_syslog_when_socket_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_socket = tmp_path / "devlog"
    fake_socket.write_text("")

    monkeypatch.setattr(log_mod, "SYSLOG_SOCKET", fake_socket)
    monkeypatch.setattr(log_mod, "SYSLOG_SOCKET_FALLBACK", tmp_path / "varrunlog")

    handler = log_mod._build_handler()
    assert isinstance(handler, logging.handlers.SysLogHandler)
    assert getattr(handler, "ident", "") == "mcubridge "


def test_build_handler_falls_back_to_stream_when_no_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(log_mod, "SYSLOG_SOCKET", tmp_path / "missing")
    monkeypatch.setattr(log_mod, "SYSLOG_SOCKET_FALLBACK", tmp_path / "missing2")

    handler = log_mod._build_handler()
    assert isinstance(handler, logging.StreamHandler)
