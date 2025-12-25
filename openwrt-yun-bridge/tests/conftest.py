"""Pytest configuration for Yun Bridge tests."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_BAUD,
    DEFAULT_STATUS_INTERVAL,
)
from yunbridge.state.context import RuntimeState, create_runtime_state


@pytest.fixture()
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Provide a clean event loop per-test to mirror historical behavior."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        with suppress(RuntimeError):
            loop.run_until_complete(loop.shutdown_asyncgens())
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture(autouse=True)
def _default_serial_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure load_runtime_config() sees a secure serial secret by default."""
    monkeypatch.setenv("YUNBRIDGE_SERIAL_SECRET", "unit-test-secret-1234")


@pytest.fixture()
def enable_event_loop_debug(
    event_loop: asyncio.AbstractEventLoop,
) -> Iterator[None]:
    """Mirror HA fixture but ensure pytest-asyncio already created the loop."""
    event_loop.set_debug(True)
    yield
    event_loop.set_debug(False)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


@pytest.fixture()
def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_SERIAL_BAUD,
        serial_safe_baud=115200,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=True,
        mqtt_cafile="/tmp/test-ca.pem",
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=DEFAULT_MQTT_TOPIC,
        allowed_commands=(),
        file_system_root="/tmp",
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        mqtt_queue_limit=8,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug_logging=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_retry_timeout=0.01,
        serial_retry_attempts=1,
        serial_shared_secret=b"unit-test-secret-1234",
        mqtt_spool_dir="/tmp/yunbridge-tests-spool",
    )


@pytest.fixture()
def runtime_state(runtime_config: RuntimeConfig) -> RuntimeState:
    state = create_runtime_state(runtime_config)
    state.link_is_synchronized = True
    return state


@pytest.fixture()
def socket_enabled() -> Iterator[None]:
    """Compat fixture so network tests work without HA plugins."""
    yield
