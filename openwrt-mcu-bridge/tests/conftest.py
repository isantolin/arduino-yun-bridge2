"""Pytest configuration for MCU Bridge tests."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

# [TEST FIX] Mock 'uci' module strictly before importing mcubridge.common.
# This simulates the OpenWrt environment where 'uci' is available.
# We use the stub from stubs/uci/ which provides proper UciException and Uci classes.
if "uci" not in sys.modules:
    # Add stubs to path and import the real stub
    _stubs_path = str(Path(__file__).parent.parent.parent / "stubs")
    if _stubs_path not in sys.path:
        sys.path.insert(0, _stubs_path)
    import uci  # This imports from stubs/uci/

    sys.modules["uci"] = uci


# [TEST FIX] Mock 'pyserial-asyncio-fast' as it is a compiled extension not available in dev env.
if "serial_asyncio_fast" not in sys.modules:
    from unittest.mock import AsyncMock

    mock_saf = MagicMock()
    # Default return value is a tuple of mocks to satisfy 'transport, proto = await ...'
    mock_saf.create_serial_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
    sys.modules["serial_asyncio_fast"] = mock_saf

import pytest
from mcubridge.config import common, settings
from mcubridge.config.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_SAFE_BAUDRATE,
)
from mcubridge.state.context import RuntimeState, create_runtime_state

_HAS_PYTEST_ASYNCIO = importlib.util.find_spec("pytest_asyncio") is not None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: mark test to run on asyncio loop")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Fallback asyncio runner when pytest-asyncio is unavailable."""
    if _HAS_PYTEST_ASYNCIO:
        return None
    if "asyncio" not in pyfuncitem.keywords:
        return None
    test_function = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_function):
        return None

    policy = pyfuncitem.funcargs.get("event_loop_policy")
    if policy is not None:
        asyncio.set_event_loop_policy(policy)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        loop.run_until_complete(test_function(**kwargs))
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except (RuntimeError, ValueError):
            pass
        loop.close()
        asyncio.set_event_loop(None)
    return True



@pytest.fixture(scope="session")
def event_loop_policy():
    """Provide uvloop event loop policy for pytest-asyncio."""
    import warnings

    import uvloop
    # Suppress deprecation warnings from uvloop internals (Python 3.16 preparation)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*AbstractEventLoopPolicy.*",
            category=DeprecationWarning,
        )
        policy = uvloop.EventLoopPolicy()
    logging.info("NOTICE: uvloop event loop policy enabled for tests.")
    return policy


@pytest.fixture(autouse=True)
def reset_logging_handlers():
    """Close and remove all logging handlers after each test to prevent ResourceWarnings."""
    yield
    root = logging.getLogger()
    for handler in root.handlers[:]:
        try:
            handler.close()
        except (OSError, RuntimeError):
            pass
        root.removeHandler(handler)



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
        serial_retry_timeout=0.05,
        serial_response_timeout=0.1,
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


@pytest.fixture
def real_config():
    import msgspec
    from mcubridge.config import settings
    raw = settings.get_default_config()
    raw["serial_shared_secret"] = b"abcd1234"
    raw["serial_retry_timeout"] = 1.0
    raw["serial_response_timeout"] = 2.0
    raw["serial_handshake_fatal_failures"] = 15
    raw["process_max_concurrent"] = 4
    config = msgspec.convert(raw, settings.RuntimeConfig, strict=False)
    return config
