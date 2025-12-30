"""Tests for RuntimeConfig normalization and validation."""

from __future__ import annotations

import os
from typing import Any

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
)
from yunbridge.rpc import protocol


def _config_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "serial_port": "/dev/null",
        "serial_baud": protocol.DEFAULT_BAUDRATE,
        "serial_safe_baud": protocol.DEFAULT_SAFE_BAUDRATE,
        "mqtt_host": "localhost",
        "mqtt_port": DEFAULT_MQTT_PORT,
        "mqtt_user": None,
        "mqtt_pass": None,
        "mqtt_tls": True,
        "mqtt_cafile": "/tmp/test-ca.pem",
        "mqtt_certfile": None,
        "mqtt_keyfile": None,
        "mqtt_topic": "yunbridge",
        "allowed_commands": (),
        "file_system_root": "/tmp",
        "process_timeout": DEFAULT_PROCESS_TIMEOUT,
        "serial_shared_secret": b"abcd1234",
    }
    base.update(overrides)
    return base


def test_runtime_config_normalizes_topic_and_paths() -> None:
    spool_relative = "relative/spool"
    expected_spool = os.path.abspath(spool_relative)
    root_input = "/tmp//bridge/test/.."
    expected_root = os.path.abspath(root_input)
    config = RuntimeConfig(
        **_config_kwargs(
            mqtt_topic="/demo//prefix/",
            file_system_root=root_input,
            mqtt_spool_dir=spool_relative,
        )
    )
    assert config.mqtt_topic == "demo/prefix"
    assert config.file_system_root == expected_root
    assert config.mqtt_spool_dir == expected_spool


def test_runtime_config_rejects_empty_topic() -> None:
    with pytest.raises(ValueError, match="mqtt_topic"):
        RuntimeConfig(**_config_kwargs(mqtt_topic="//"))


def test_runtime_config_rejects_non_positive_status_interval() -> None:
    with pytest.raises(ValueError, match="status_interval"):
        RuntimeConfig(**_config_kwargs(status_interval=0))


def test_runtime_config_requires_watchdog_interval_when_enabled() -> None:
    with pytest.raises(ValueError, match="watchdog_interval"):
        RuntimeConfig(
            **_config_kwargs(
                watchdog_enabled=True,
                watchdog_interval=0.0,
            )
        )


def test_runtime_config_rejects_non_positive_fatal_threshold() -> None:
    with pytest.raises(ValueError, match="serial_handshake_fatal_failures"):
        RuntimeConfig(
            **_config_kwargs(
                serial_handshake_fatal_failures=0,
            )
        )
