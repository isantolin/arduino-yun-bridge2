import importlib
import sys
import types
import logging
import msgspec
from typing import Any, Self

import pytest

from mcubridge import common, const
from mcubridge.config import settings
from mcubridge.rpc import protocol


def _runtime_config_kwargs(**overrides: Any) -> dict[str, Any]:
    """Helper to build minimum valid kwargs for RuntimeConfig."""
    base = {
        "serial_port": "/dev/ttyATH0",
        "serial_baud": protocol.DEFAULT_BAUDRATE,
        "serial_safe_baud": protocol.DEFAULT_SAFE_BAUDRATE,
        "mqtt_host": "localhost",
        "mqtt_port": 1883,
        "mqtt_user": None,
        "mqtt_pass": None,
        "mqtt_tls": True,
        "mqtt_cafile": None,
        "mqtt_certfile": None,
        "mqtt_keyfile": None,
        "mqtt_topic": "br",
        "allowed_commands": ("uptime",),
        "file_system_root": "/tmp",
        "process_timeout": 30,
        "serial_shared_secret": b"s_e_c_r_e_t_mock_1234",
    }
    base.update(overrides)
    return base


def _install_dummy_uci_module(monkeypatch: pytest.MonkeyPatch, section: dict[str, Any]) -> None:
    class _DummyCursor:
        def __init__(self, payload: dict[str, Any]):
            self._payload = payload

        def __enter__(self) -> Self:
            return self

        def __exit__(
            self,
            exc_type: Any,
            exc: Any,
            exc_tb: Any,
        ) -> bool:  # pragma: no cover - simple context manager
            return False

        def get_all(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return self._payload

    module = types.SimpleNamespace(
        Uci=lambda: _DummyCursor(section),
        UciException=RuntimeError,
    )

    monkeypatch.setitem(sys.modules, "uci", module)
    importlib.reload(common)
    importlib.reload(settings)


def test_load_runtime_config_applies_env_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
):
    raw_config = {
        "serial_port": "/dev/custom",
        "serial_baud": "57600",
        "serial_safe_baud": "9600",
        "mqtt_host": "broker",
        "mqtt_port": "321",
        "mqtt_user": " user ",
        "mqtt_pass": " pass ",
        "mqtt_tls": "1",
        "mqtt_cafile": " /etc/cafile ",
        "mqtt_certfile": " ",
        "mqtt_keyfile": "",
        "mqtt_topic": " custom/topic ",
        "allowed_commands": "  ls  ECHO ls  ",
        "file_system_root": "/data",
        "allow_non_tmp_paths": "1",
        "process_timeout": "60",
        "mqtt_queue_limit": "1",
        "reconnect_delay": "7",
        "status_interval": "5",
        "console_queue_limit_bytes": "4096",
        "mailbox_queue_limit": "3",
        "mailbox_queue_bytes_limit": "512",
        "serial_retry_timeout": "1.5",
        "serial_response_timeout": "1.0",
        "serial_retry_attempts": "1",
        "serial_shared_secret": " envsecret ",
        "watchdog_enabled": "1",
        "watchdog_interval": "0.2",
    }

    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "test"))

    config = settings.load_runtime_config()

    assert config.serial_port == "/dev/custom"
    assert config.serial_baud == 57600
    assert config.mqtt_host == "broker"
    assert config.mqtt_port == 321
    assert config.mqtt_user == "user"
    assert config.mqtt_pass == "pass"
    assert config.mqtt_tls is True
    assert config.mqtt_cafile == "/etc/cafile"
    assert config.mqtt_certfile is None
    assert config.mqtt_keyfile is None
    assert config.mqtt_topic == "custom/topic"
    assert config.allowed_commands == ("ls", "echo")
    assert config.allowed_policy is not None
    assert config.allowed_policy.is_allowed("ls --help")
    assert config.file_system_root == "/data"
    assert config.process_timeout == 60
    assert config.mqtt_queue_limit == 1
    assert config.reconnect_delay == 7
    assert config.status_interval == 5
    assert config.debug_logging is False
    assert config.console_queue_limit_bytes == 4096
    assert config.mailbox_queue_limit == 3
    assert config.mailbox_queue_bytes_limit == 512
    assert config.serial_retry_timeout == 1.5
    assert config.serial_response_timeout == 3.0
    assert config.serial_retry_attempts == 1
    assert config.watchdog_enabled is True
    assert config.watchdog_interval == 0.5
    assert config.tls_enabled is True
    assert config.serial_shared_secret == b"envsecret"
    assert config.metrics_enabled is False
    assert config.metrics_host == const.DEFAULT_METRICS_HOST
    assert config.metrics_port == const.DEFAULT_METRICS_PORT


def test_load_runtime_config_metrics(monkeypatch: pytest.MonkeyPatch):
    raw_config = {
        "serial_port": "/dev/ttyS1",
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
        "mqtt_host": "broker",
        "mqtt_port": "8883",
        "mqtt_tls": "1",
        "mqtt_cafile": "/etc/ca.pem",
        "mqtt_topic": "br",
        "allowed_commands": "uptime",
        "file_system_root": "/tmp",
        "process_timeout": "10",
        "serial_shared_secret": " s_e_c_r_e_t_mock ",
        "metrics_enabled": "0",
        "metrics_host": "0.0.0.0",
        "metrics_port": "9200",
    }

    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "test"))

    config = settings.load_runtime_config()

    # OpenWrt policy: ENV variables must not override persisted UCI config.
    assert config.metrics_enabled is False
    assert config.metrics_host == "0.0.0.0"
    assert config.metrics_port == 9200


def test_load_runtime_config_overrides_non_tmp_paths_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    raw_config = {
        "serial_port": "/dev/ttyS1",
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
        "mqtt_host": "broker",
        "mqtt_port": "8883",
        "mqtt_tls": "1",
        "mqtt_cafile": "/etc/ca.pem",
        "mqtt_topic": "br",
        "allowed_commands": "uptime",
        "file_system_root": "/data",
        "mqtt_spool_dir": "/data/spool",
        "allow_non_tmp_paths": "0",
        "process_timeout": "10",
        "serial_shared_secret": " s_e_c_r_e_t_mock ",
    }

    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "test"))

    # Strictly enforcing flash protection now returns default config on validation error.
    config = settings.load_runtime_config()
    assert config.file_system_root == const.DEFAULT_FILE_SYSTEM_ROOT


def test_load_runtime_config_allows_empty_mqtt_user_value(
    monkeypatch: pytest.MonkeyPatch,
):
    raw_config = {
        "serial_port": "/dev/env",
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
        "mqtt_tls": "1",
        "mqtt_host": "broker",
        "mqtt_port": "8883",
        "mqtt_cafile": "/etc/cafile",
        "mqtt_user": "   ",
        "serial_shared_secret": " envsecret ",
    }

    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "test"))

    config = settings.load_runtime_config()

    assert config.mqtt_user is None


def test_load_runtime_config_prefers_uci_config(
    monkeypatch: pytest.MonkeyPatch,
):
    uci_config = {
        "serial_port": "/dev/uci",
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
        "debug": "1",
        "mqtt_tls": "0",
        "serial_shared_secret": " s_e_c_r_e_t_mock ",
    }

    monkeypatch.setattr(settings, "get_uci_config", lambda: uci_config)

    def _unexpected_default() -> dict[str, Any]:
        raise AssertionError("default config should not be used")

    monkeypatch.setattr(settings, "get_default_config", _unexpected_default)

    config = settings.load_runtime_config()

    assert config.serial_port == "/dev/uci"
    assert config.debug_logging is True
    assert config.mqtt_tls is False
    assert config.tls_enabled is False


def test_load_runtime_config_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
):
    def _uci_failure() -> dict[str, Any]:
        raise OSError("uci unavailable")

    default_config = {
        "serial_port": "/dev/default",
        "serial_baud": 115200,
        "serial_safe_baud": 9600,
        "mqtt_host": "localhost",
        "mqtt_port": 1883,
        "mqtt_topic": "br",
        "file_system_root": "/tmp",
        "mqtt_spool_dir": "/tmp/spool",
        "process_timeout": 30,
        "debug_logging": False,
        "mqtt_tls": True,
        "mqtt_user": None,
        "mqtt_pass": None,
        "mqtt_cafile": "/etc/cafile",
        "mqtt_certfile": None,
        "mqtt_keyfile": None,
        "mqtt_queue_limit": 1,
        "serial_retry_timeout": 4.0,
        "serial_response_timeout": 2.0,
        "serial_retry_attempts": 1,
        "allowed_commands": ("*",),
        "serial_shared_secret": b"defaultsecret",
        "console_queue_limit_bytes": 1024,
        "mailbox_queue_limit": 10,
        "mailbox_queue_bytes_limit": 1024,
        "pending_pin_request_limit": 5,
        "reconnect_delay": 5,
        "status_interval": 10,
        "bridge_summary_interval": 0.0,
        "bridge_handshake_interval": 0.0,
        "metrics_enabled": False,
        "metrics_host": "127.0.0.1",
        "metrics_port": 9100,
        "watchdog_enabled": False,
        "watchdog_interval": 5.0,
        "allow_non_tmp_paths": False,
        "mqtt_tls_insecure": False,
        "file_write_max_bytes": 1024,
        "file_storage_quota_bytes": 1024,
        "process_max_output_bytes": 1024,
        "process_max_concurrent": 1,
    }

    monkeypatch.setattr(settings, "get_uci_config", _uci_failure)
    monkeypatch.setattr(settings, "get_default_config", lambda: default_config)
    config = settings.load_runtime_config()

    assert config.serial_port == "/dev/default"
    assert config.serial_baud == 115200
    assert config.mqtt_tls is True
    assert config.mqtt_user is None
    assert config.mqtt_pass is None
    assert config.mqtt_cafile == "/etc/cafile"
    assert config.mqtt_certfile is None
    assert config.mqtt_keyfile is None
    assert config.mqtt_queue_limit == 1
    assert config.serial_retry_timeout == 4.0
    assert config.serial_response_timeout == 8.0  # max(2.0, 4.0 * 2)
    assert config.serial_retry_attempts == 1
    assert config.allowed_policy is not None
    assert config.allowed_policy.allow_all is True
    assert config.watchdog_enabled is False
    assert config.serial_shared_secret == b"defaultsecret"


def test_get_uci_config_flattens_nested_structures(
    monkeypatch: pytest.MonkeyPatch,
):
    section = {
        ".name": "general",
        ".type": "general",
        "mqtt_host": "remote.example",
        "mqtt_port": "1884",
        "mqtt_tls": "0",
    }
    _install_dummy_uci_module(monkeypatch, section)
    config = common.get_uci_config()
    assert config["mqtt_host"] == "remote.example"
    assert config["mqtt_port"] == "1884"
    assert config["mqtt_tls"] == "0"


def test_get_uci_config_handles_value_wrappers(
    monkeypatch: pytest.MonkeyPatch,
):
    section = {
        "serial_port": ["/dev/ttyS1"],
        "serial_baud": "115200",
    }
    _install_dummy_uci_module(monkeypatch, section)
    config = common.get_uci_config()
    assert config["serial_port"] == "/dev/ttyS1"
    assert config["serial_baud"] == "115200"


def test_load_runtime_config_parses_watchdog(monkeypatch: pytest.MonkeyPatch):
    raw_config = common.get_default_config()
    raw_config.update(
        {
            "serial_port": "/dev/ttyS1",
            "serial_baud": str(protocol.DEFAULT_BAUDRATE),
            "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
            "mqtt_host": "broker",
            "mqtt_port": "8883",
            "mqtt_tls": "1",
            "mqtt_cafile": "/etc/ca.pem",
            "mqtt_topic": "br",
            "allowed_commands": "uptime",
            "file_system_root": "/tmp",
            "process_timeout": "10",
            "serial_shared_secret": " s_e_c_r_e_t_mock ",
            "watchdog_enabled": "1",
            "watchdog_interval": "0.2",
        }
    )

    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "test"))

    config = settings.load_runtime_config()
    assert config.watchdog_enabled is True
    assert config.watchdog_interval == 0.5


def test_structured_formatter_trims_prefix_and_serialises_extra():
    from mcubridge.config.logging import StructuredLogFormatter

    formatter = StructuredLogFormatter()
    record = logging.LogRecord(
        name="mcubridge.sub",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.custom = "value"

    payload = msgspec.json.decode(formatter.format(record).encode("utf-8"))

    assert payload["logger"] == "sub"
    assert payload["message"] == "hello"
    assert payload["extra"]["custom"] == "value"
    assert "ts" in payload


def test_structured_formatter_handles_bytes():
    from mcubridge.config.logging import StructuredLogFormatter

    formatter = StructuredLogFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="binary",
        args=(),
        exc_info=None,
    )
    record.data = b"\xde\xad\xbe\xef"

    payload = msgspec.json.decode(formatter.format(record).encode("utf-8"))
    assert payload["extra"]["data"] == "[DE AD BE EF]"
