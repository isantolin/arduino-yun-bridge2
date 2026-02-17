"""Tests for RuntimeConfig loader and utility functions."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from mcubridge.config import common, settings
from mcubridge.protocol import protocol


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
        "serial_retry_timeout": "0.5",
        "serial_response_timeout": "1.5",
        "serial_retry_attempts": "1",
        "serial_shared_secret": " envsecret ",
        "watchdog_enabled": "1",
        "watchdog_interval": "0.5",
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
    assert config.allowed_commands == ("echo", "ls")
    assert config.file_system_root == "/data"
    assert config.process_timeout == 60
    assert config.mqtt_queue_limit == 1
    assert config.reconnect_delay == 7
    assert config.status_interval == 5
    assert config.console_queue_limit_bytes == 4096
    assert config.mailbox_queue_limit == 3
    assert config.mailbox_queue_bytes_limit == 512
    assert config.serial_retry_timeout == 0.5
    assert config.serial_response_timeout == 1.5
    assert config.serial_retry_attempts == 1
    assert config.serial_shared_secret == b"envsecret"
    assert config.watchdog_enabled is True
    assert config.watchdog_interval == 0.5


def test_load_runtime_config_metrics(monkeypatch: pytest.MonkeyPatch):
    raw_config = {
        "metrics_enabled": "1",
        "metrics_host": "0.0.0.0",
        "metrics_port": "9999",
        "bridge_summary_interval": "10.5",
        "bridge_handshake_interval": "20",
    }
    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "test"))

    config = settings.load_runtime_config()
    assert config.metrics_enabled is True
    assert config.metrics_host == "0.0.0.0"
    assert config.metrics_port == 9999
    assert config.bridge_summary_interval == 10.5
    assert config.bridge_handshake_interval == 20.0


def test_load_runtime_config_overrides_non_tmp_paths_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    raw_config = {
        "mqtt_spool_dir": "/var/spool/mcu",
        "file_system_root": "/var/lib/mcu",
        "allow_non_tmp_paths": "0",
    }
    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "test"))

    config = settings.load_runtime_config()
    # Spool must be under /tmp if not allowed
    assert config.mqtt_spool_dir.startswith("/tmp")
    assert config.file_system_root.startswith("/tmp")


def test_load_runtime_config_allows_empty_mqtt_user_value(
    monkeypatch: pytest.MonkeyPatch,
):
    raw_config = {
        "mqtt_user": "",
        "mqtt_pass": " ",
    }
    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "test"))

    config = settings.load_runtime_config()
    assert config.mqtt_user is None
    assert config.mqtt_pass is None


def test_load_runtime_config_prefers_uci_config(monkeypatch: pytest.MonkeyPatch):
    raw_config = {"serial_port": "/dev/uci"}
    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "uci"))

    config = settings.load_runtime_config()
    assert config.serial_port == "/dev/uci"


def test_load_runtime_config_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
):
    def _uci_failure() -> dict[str, Any]:
        raise OSError("uci unavailable")

    monkeypatch.setattr(settings, "get_uci_config", _uci_failure)

    # We must ensure get_default_config returns a valid config or convert will fail
    # Default is valid by definition.
    config = settings.load_runtime_config()
    from mcubridge.config import const
    assert config.serial_port == const.DEFAULT_SERIAL_PORT


def test_get_uci_config_flattens_nested_structures(monkeypatch: pytest.MonkeyPatch):
    # Mock UCI result with list of values (e.g. allowed_commands)
    def _uci_mock() -> dict[str, Any]:
        return {
            "allowed_commands": ["ls", "uptime"],
            "mqtt_topic": "br",
        }

    monkeypatch.setattr(settings, "get_uci_config", _uci_mock)
    raw, _ = settings._load_raw_config()
    assert raw["allowed_commands"] == ["ls", "uptime"]


def test_get_uci_config_handles_value_wrappers(monkeypatch: pytest.MonkeyPatch):
    # Mocking UCI internal list handling
    def _uci_mock() -> dict[str, Any]:
        return {"debug": "1"}

    monkeypatch.setattr(settings, "get_uci_config", _uci_mock)
    config = settings.load_runtime_config()
    assert config.debug_logging is True


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
            "watchdog_interval": "0.5",
        }
    )

    monkeypatch.setattr(settings, "_load_raw_config", lambda: (raw_config, "test"))

    config = settings.load_runtime_config()
    assert config.watchdog_enabled is True
    assert config.watchdog_interval == 0.5


def test_structured_formatter_trims_prefix_and_serialises_extra():
    from mcubridge.config.logging import StructuredFormatter

    formatter = StructuredFormatter()
    record = logging.LogRecord(
        name="mcubridge.service.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.extra_field = "data"

    output = formatter.format(record)
    assert '"logger":"service.test"' in output
    assert '"extra_field":"data"' in output


def test_structured_formatter_handles_bytes():
    from mcubridge.config.logging import StructuredFormatter

    formatter = StructuredFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="data",
        args=(),
        exc_info=None,
    )
    record.raw_bytes = b"\x01\x02"

    output = formatter.format(record)
    assert '"raw_bytes":"[01 02]"' in output
