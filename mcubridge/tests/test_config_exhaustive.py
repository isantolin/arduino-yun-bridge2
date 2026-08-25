"""Exhaustive tests for mcubridge.config.logging and mcubridge.config.settings modules. [SIL-2]"""

from __future__ import annotations
from mcubridge.config import settings

from typing import Any
import logging
from unittest.mock import MagicMock, patch

import pytest
from mcubridge.config.logging import configure_logging, hexdump_processor
from mcubridge.config.settings import (
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


def test_configure_logging_env_debug() -> None:
    with patch.dict("os.environ", {"MCUBRIDGE_DEBUG": "1"}):
        configure_logging()
        assert logging.getLogger().level == logging.DEBUG
    with patch.dict("os.environ", {"MCUBRIDGE_DEBUG": "0"}):
        configure_logging()
        assert logging.getLogger().level == logging.INFO


def test_configure_logging_stream_override() -> None:
    cfg = pb.RuntimeConfig(debug=True)
    with patch.dict("os.environ", {"MCUBRIDGE_LOG_STREAM": "1"}):
        configure_logging(cfg)
        assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_syslog_paths() -> None:
    cfg = pb.RuntimeConfig(debug=False)
    # /dev/log
    with patch.dict("os.environ", {}, clear=True):
        with patch("pathlib.Path.exists", side_effect=lambda: True):
            mock_handler = MagicMock()
            mock_handler.level = 0
            with patch("mcubridge.config.logging.SysLogHandler", return_value=mock_handler) as mock_syslog:
                configure_logging(cfg)
                assert mock_syslog.called

    # /var/run/log
    def exists_var_run(self_path: Any) -> bool:
        return str(self_path) == "/var/run/log"

    with patch.dict("os.environ", {}, clear=True):
        with patch("pathlib.Path.exists", exists_var_run):
            mock_handler = MagicMock()
            mock_handler.level = 0
            with patch("mcubridge.config.logging.SysLogHandler", return_value=mock_handler) as mock_syslog:
                configure_logging()
                assert mock_syslog.called

    # No syslog
    with patch.dict("os.environ", {}, clear=True):
        with patch("pathlib.Path.exists", return_value=False):
            configure_logging()
            assert any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers)


# =============================================================================
# 2. Tests for mcubridge.config.settings
# =============================================================================


def test_runtime_config_factory() -> None:
    factory = getattr(settings, "_runtime_config_factory")
    prebuilt = pb.RuntimeConfig(topic_prefix="test")
    res = factory(pb_msg=prebuilt)
    assert res == prebuilt

    res2 = factory(serial_shared_secret="my_secret")
    assert res2.serial_shared_secret == b"my_secret"


def test_get_config_source() -> None:
    assert get_config_source() in ("uci", "defaults", "cli")


def test_coerce_value() -> None:
    from google.protobuf.descriptor import FieldDescriptor

    coerce_fn = getattr(settings, "_coerce_value")

    assert coerce_fn(None, FieldDescriptor.TYPE_STRING) is None

    # String & Path
    assert coerce_fn("  hello  ", FieldDescriptor.TYPE_STRING) == "hello"
    assert coerce_fn("   ", FieldDescriptor.TYPE_STRING) is None
    assert "/tmp" in coerce_fn("/tmp", FieldDescriptor.TYPE_STRING, "cloud_spool_dir")

    # Integer types
    assert coerce_fn("123", FieldDescriptor.TYPE_UINT32) == 123
    assert coerce_fn("invalid", FieldDescriptor.TYPE_UINT32) == 0

    # Float types
    assert coerce_fn("45.6", FieldDescriptor.TYPE_FLOAT) == 45.6
    assert coerce_fn("invalid", FieldDescriptor.TYPE_FLOAT) == 0.0

    # Bool types
    assert coerce_fn(True, FieldDescriptor.TYPE_BOOL) is True
    assert coerce_fn("yes", FieldDescriptor.TYPE_BOOL) is True
    assert coerce_fn("off", FieldDescriptor.TYPE_BOOL) is False

    # Bytes types
    assert coerce_fn(b"bytes", FieldDescriptor.TYPE_BYTES) == b"bytes"
    assert coerce_fn("str_bytes", FieldDescriptor.TYPE_BYTES) == b"str_bytes"


def test_load_runtime_config_uci_error_fallback() -> None:
    with patch("mcubridge.config.settings.get_uci_config", side_effect=OSError("UCI locked")):
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


def test_load_runtime_config_uci_invalid_fatal() -> None:
    with patch("mcubridge.config.settings._load_raw_config", return_value=({"topic_prefix": ""}, "uci")):
        with pytest.raises(RuntimeError, match="Invalid system configuration"):
            load_runtime_config()


def test_load_runtime_config_cli_invalid_fatal() -> None:
    with patch("mcubridge.config.settings._load_raw_config", return_value=({"topic_prefix": ""}, "defaults")):
        with pytest.raises(ValueError, match="topic_prefix"):
            load_runtime_config(overrides={"topic_prefix": ""})
