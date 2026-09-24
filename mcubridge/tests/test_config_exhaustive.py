"""Exhaustive tests for mcubridge.config.logging and mcubridge.config.settings modules. [SIL-2]"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from mcubridge.config.logging import configure_logging, hexdump_processor
from mcubridge.config import settings
from mcubridge.config.settings import (
    RuntimeConfig,
    _coerce_value,
    get_config_source,
    load_runtime_config,
)
from mcubridge.protocol import mcubridge_pb2 as pb


# =============================================================================
# 1. Tests for mcubridge.config.logging
# =============================================================================


def test_hexdump_processor_bytes() -> None:
    event_dict = {
        "payload": b"\xde\xad\xbe\xef",
        "empty": b"",
        "bytearray": bytearray(b"\x01\x02"),
        "memoryview": memoryview(b"\x03\x04"),
        "text": "normal_string",
    }
    processed = hexdump_processor(None, "event", event_dict)
    assert processed["payload"] == "[DE AD BE EF]"
    assert processed["empty"] == "[]"
    assert processed["bytearray"] == "[01 02]"
    assert processed["memoryview"] == "[03 04]"
    assert processed["text"] == "normal_string"


def test_configure_logging_debug_and_console() -> None:
    configure_logging(debug=True, console=True)
    assert logging.getLogger().level == logging.DEBUG
    configure_logging(debug=False, console=False)
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_env_debug(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {"MCUBRIDGE_DEBUG": "1"})
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG
    mocker.patch.dict("os.environ", {"MCUBRIDGE_DEBUG": "0"})
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_stream_override(mocker: MockerFixture) -> None:
    cfg = pb.RuntimeConfig(debug=True)
    mocker.patch.dict("os.environ", {"MCUBRIDGE_LOG_STREAM": "1"})
    configure_logging(cfg)
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_syslog_paths(mocker: MockerFixture) -> None:
    cfg = pb.RuntimeConfig(debug=False)
    # /dev/log
    mocker.patch.dict("os.environ", {}, clear=True)
    mocker.patch("pathlib.Path.exists", side_effect=lambda: True)
    mock_handler = MagicMock()
    mock_handler.level = 0
    mock_syslog = mocker.patch("mcubridge.config.logging.SysLogHandler", return_value=mock_handler)
    configure_logging(cfg)
    assert mock_syslog.called

    # /var/run/log
    def exists_var_run(self_path: Any) -> bool:
        return str(self_path) == "/var/run/log"

    mocker.patch.dict("os.environ", {}, clear=True)
    mocker.patch("pathlib.Path.exists", exists_var_run)
    mock_handler2 = MagicMock()
    mock_handler2.level = 0
    mock_syslog2 = mocker.patch("mcubridge.config.logging.SysLogHandler", return_value=mock_handler2)
    configure_logging()
    assert mock_syslog2.called

    # No syslog
    mocker.patch.dict("os.environ", {}, clear=True)
    mocker.patch("pathlib.Path.exists", return_value=False)
    configure_logging()
    assert any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers)


# =============================================================================
# 2. Tests for mcubridge.config.settings
# =============================================================================


def test_runtime_config_factory() -> None:
    prebuilt = pb.RuntimeConfig(topic_prefix="test")
    res = RuntimeConfig(pb_msg=prebuilt)
    assert res == prebuilt

    res2 = RuntimeConfig(serial_shared_secret="my_secret")
    assert res2.serial_shared_secret == b"my_secret"


def test_get_config_source() -> None:
    assert get_config_source() in ("uci", "defaults", "cli")


def test_coerce_value() -> None:
    from google.protobuf.descriptor import FieldDescriptor

    assert _coerce_value(None, FieldDescriptor.TYPE_STRING) is None

    # String & Path
    assert _coerce_value("  hello  ", FieldDescriptor.TYPE_STRING) == "hello"
    assert _coerce_value("   ", FieldDescriptor.TYPE_STRING) is None
    assert "/tmp" in _coerce_value("/tmp", FieldDescriptor.TYPE_STRING, "cloud_spool_dir")

    # Integer types
    assert _coerce_value("123", FieldDescriptor.TYPE_UINT32) == 123
    assert _coerce_value("invalid", FieldDescriptor.TYPE_UINT32) == 0

    # Float types
    assert _coerce_value("45.6", FieldDescriptor.TYPE_FLOAT) == 45.6
    assert _coerce_value("invalid", FieldDescriptor.TYPE_FLOAT) == 0.0

    # Bool types
    assert _coerce_value(True, FieldDescriptor.TYPE_BOOL) is True
    assert _coerce_value("yes", FieldDescriptor.TYPE_BOOL) is True
    assert _coerce_value("off", FieldDescriptor.TYPE_BOOL) is False

    # Bytes types
    assert _coerce_value(b"bytes", FieldDescriptor.TYPE_BYTES) == b"bytes"
    assert _coerce_value("str_bytes", FieldDescriptor.TYPE_BYTES) == b"str_bytes"


def test_normalize_config_dict() -> None:
    norm, secret = settings._normalize_config_dict(
        {
            "serial_shared_secret": "my_secret",
            "serial_port": "tcp://192.168.122.1:9000",
            "cloud_enabled": "1",
            "cloud_tls": "true",
            "watchdog_enabled": "0",
            "watchdog_interval": "1.5",
            "allowed_commands": "reboot ls",
            "topic_authorization": {"datastore_get": True, "datastore_put": "true"},
        }
    )
    assert secret == b"my_secret"
    assert norm["serial_port"] == "tcp://192.168.122.1:9000"
    assert norm["cloud_enabled"] is True
    assert norm["cloud_tls"] is True
    assert norm["watchdog_enabled"] is False
    assert norm["watchdog_interval"] == 1.5
    assert norm["allowed_commands"] == ["reboot", "ls"]
    assert norm["topic_authorization"]["datastore_get"] is True
    assert norm["topic_authorization"]["datastore_put"] is True

    # Test other network prefixes
    norm_wifi, _ = settings._normalize_config_dict({"serial_port": "wifi://10.0.0.5:8080"})
    assert norm_wifi["serial_port"] == "wifi://10.0.0.5:8080"
    norm_socket, _ = settings._normalize_config_dict({"serial_port": "socket://127.0.0.1:4000"})
    assert norm_socket["serial_port"] == "socket://127.0.0.1:4000"


def test_load_runtime_config_uci_error_fallback(mocker: MockerFixture) -> None:
    mocker.patch("mcubridge.config.settings.get_uci_config", side_effect=OSError("UCI locked"))
    cfg = load_runtime_config()
    assert cfg.topic_prefix == "br"
    assert get_config_source() == "defaults"


def test_load_runtime_config_with_overrides() -> None:
    overrides = {
        "topic_prefix": "custom_prefix",
        "allowed_commands": "cat ls grep",
        "cloud_allow_digital_read": "1",
    }
    cfg = load_runtime_config(overrides=overrides)
    assert cfg.topic_prefix == "custom_prefix"
    assert get_config_source() == "cli"
    assert cfg.topic_authorization.digital_read is True


def test_load_runtime_config_uci_invalid_fatal(mocker: MockerFixture) -> None:
    mocker.patch("mcubridge.config.settings._load_raw_config", return_value=({"topic_prefix": ""}, "uci"))
    with pytest.raises(RuntimeError, match="Invalid system configuration"):
        load_runtime_config()


def test_load_runtime_config_cli_invalid_fatal(mocker: MockerFixture) -> None:
    mocker.patch("mcubridge.config.settings._load_raw_config", return_value=({"topic_prefix": ""}, "defaults"))
    with pytest.raises(ValueError, match="topic_prefix must contain"):
        load_runtime_config(overrides={"topic_prefix": ""})
