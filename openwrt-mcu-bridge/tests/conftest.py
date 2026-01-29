"""Pytest configuration for MCU Bridge tests."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

# [TEST FIX] Mock 'uci' module strictly before importing mcubridge.common.
# This simulates the OpenWrt environment where 'uci' is available.
if "uci" not in sys.modules:
    sys.modules["uci"] = MagicMock()

# [TEST FIX] Mock 'pyserial-asyncio-fast' as it is a compiled extension not available in dev env.
if "serial_asyncio_fast" not in sys.modules:
    from unittest.mock import AsyncMock

    mock_saf = MagicMock()
    # Default return value is a tuple of mocks to satisfy 'transport, proto = await ...'
    mock_saf.create_serial_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
    sys.modules["serial_asyncio_fast"] = mock_saf

import pytest

from mcubridge import common
from mcubridge.config import settings
from mcubridge.config.settings import RuntimeConfig
from mcubridge.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.rpc import protocol
from mcubridge.rpc.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_SAFE_BAUDRATE,
)
from mcubridge.state.context import RuntimeState, create_runtime_state


@pytest.fixture(autouse=True)
def logging_mock_level_fix():
    """Ensure all handlers have a numeric level to avoid comparisons with MagicMock."""
    original_handlers = []
    # Capture existing loggers
    loggers = [logging.getLogger(name) for name in logging.root.manager.loggerDict]
    loggers.append(logging.getLogger())  # Root logger

    for logger in loggers:
        for handler in logger.handlers:
            if isinstance(handler.level, MagicMock):
                original_handlers.append((handler, handler.level))
                handler.level = logging.NOTSET

    yield

    # Restore (though usually not necessary for tests)
    for handler, level in original_handlers:
        handler.level = level


@pytest.fixture(autouse=True)
def _default_serial_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure load_runtime_config() sees a secure serial secret by default.

    Settings are UCI-only, so we inject a deterministic UCI payload for tests.
    """

    def _test_uci_config() -> dict[str, str]:
        config = common.get_default_config()
        config["serial_shared_secret"] = "s_e_c_r_e_t_mock"
        return config

    monkeypatch.setattr(settings, "get_uci_config", _test_uci_config)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


@pytest.fixture()
def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=DEFAULT_BAUDRATE,
        serial_safe_baud=DEFAULT_SAFE_BAUDRATE,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_tls=True,
        mqtt_cafile="/tmp/test-ca.pem",
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
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
        serial_shared_secret=b"s_e_c_r_e_t_mock",
        mqtt_spool_dir="/tmp/mcubridge-tests-spool",
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
