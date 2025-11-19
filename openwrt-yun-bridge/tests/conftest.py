"""Pytest configuration for Yun Bridge tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.state.context import RuntimeState, create_runtime_state

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


@pytest.fixture()
def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=False,
        mqtt_cafile=None,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic="br",
        allowed_commands=[],
        file_system_root="/tmp",
        process_timeout=10,
        mqtt_queue_limit=8,
        reconnect_delay=5,
        status_interval=5,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_retry_timeout=0.01,
        serial_retry_attempts=1,
    )


@pytest.fixture()
def runtime_state(runtime_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(runtime_config)
