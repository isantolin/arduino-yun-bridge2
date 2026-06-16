"""Pytest configuration for MCU Bridge tests."""

from __future__ import annotations

import asyncio
from asyncio import events as asyncio_events
from collections.abc import Iterator
import gc
import importlib.util
import inspect
import logging
import os
from pathlib import Path
import shutil
import sys
import time
from typing import cast

import msgspec
import pytest
import structlog

from mcubridge.config import common, settings
import mcubridge.config.common
import mcubridge.config.const
from mcubridge.config.const import (
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
)
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_MQTT_PORT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SAFE_BAUDRATE,
)
import mcubridge.protocol.structures
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport

# Setup paths for local imports and stubs (placed after all imports to satisfy E402)
_stubs_path = str(Path(__file__).parent.parent / "stubs")
if _stubs_path not in sys.path:
    sys.path.insert(0, _stubs_path)

_package_root = str(Path(__file__).resolve().parents[1])
if _package_root not in sys.path:
    sys.path.insert(0, _package_root)

# ==============================================================================
# GLOBAL TEST PATH ISOLATION PATCHING
# ==============================================================================
# This monkeypatches the default directories for both direct RuntimeConfig(...)
# calls and settings load functions (get_default_config) to ensure that each
# test case runs in its own unique, isolated /tmp directory.
# A cache is used to ensure stability (same paths) within a single test case,
# which is reset between tests by the isolate_test_paths fixture.

_test_paths: dict[str, str | None] = {
    "spool": None,
    "fs": None,
}


def get_unique_test_spool() -> str:
    if _test_paths["spool"] is None:
        _test_paths["spool"] = f"/tmp/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    return _test_paths["spool"]


def get_unique_test_fs() -> str:
    if _test_paths["fs"] is None:
        _test_paths["fs"] = f"/tmp/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    return _test_paths["fs"]


OriginalRuntimeConfig = mcubridge.protocol.structures.RuntimeConfig
original_convert = msgspec.convert
original_get_default_config = mcubridge.config.common.get_default_config


class PatchedRuntimeConfig:
    def __new__(cls, *args, **kwargs):
        default_spool = mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR
        default_fs = mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT
        if (
            "mqtt_spool_dir" not in kwargs
            or kwargs["mqtt_spool_dir"] == "/tmp/mcubridge/spool"
            or kwargs["mqtt_spool_dir"] == default_spool
        ):
            kwargs["mqtt_spool_dir"] = get_unique_test_spool()
        if (
            "file_system_root" not in kwargs
            or kwargs["file_system_root"] == "/tmp/mcubridge"
            or kwargs["file_system_root"] == default_fs
        ):
            kwargs["file_system_root"] = get_unique_test_fs()
        return OriginalRuntimeConfig(*args, **kwargs)


def patched_convert(obj, type_arg, *args, **kwargs):
    if type_arg is PatchedRuntimeConfig:
        type_arg = OriginalRuntimeConfig
    return original_convert(obj, type_arg, *args, **kwargs)


def patched_get_default_config():
    cfg = original_get_default_config()
    cfg["mqtt_spool_dir"] = get_unique_test_spool()
    cfg["file_system_root"] = get_unique_test_fs()
    return cfg


msgspec.convert = patched_convert
mcubridge.protocol.structures.RuntimeConfig = PatchedRuntimeConfig
mcubridge.config.common.get_default_config = patched_get_default_config
# ==============================================================================


# [TEST FIX] Configure structlog purely natively but route to logging for caplog compatibility.
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
    cache_logger_on_first_use=False,
)

_HAS_PYTEST_ASYNCIO = importlib.util.find_spec("pytest_asyncio") is not None


def _get_event_loop_policy() -> object:
    private_getter = getattr(asyncio_events, "_get_event_loop_policy", None)
    if private_getter is not None:
        return private_getter()

    policy = getattr(asyncio_events, "_event_loop_policy", None)
    if policy is None:
        init_policy = getattr(asyncio_events, "_init_event_loop_policy", None)
        if init_policy is None:
            return None
        init_policy()
        policy = getattr(asyncio_events, "_event_loop_policy", None)
    return policy


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
        getattr(asyncio, "set_event_loop_policy")(cast(asyncio.AbstractEventLoopPolicy, policy))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        kwargs = {name: pyfuncitem.funcargs[name] for name in getattr(pyfuncitem, "_fixtureinfo").argnames}
        loop.run_until_complete(test_function(**kwargs))
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except (RuntimeError, ValueError) as e:
            logging.debug("Loop asyncgen shutdown notice: %s", e)
        loop.close()
        asyncio.set_event_loop(None)
    return True


@pytest.fixture(autouse=True)
def force_gc_cleanup():
    """Ensure all resources are released after each test to reach zero warnings."""
    yield
    # Close any stale event loop left by asyncio.run() or explicit set_event_loop
    policy = _get_event_loop_policy()
    loop = getattr(getattr(policy, "_local", None), "_loop", None)
    if loop is not None and not loop.is_closed():
        loop.close()
    asyncio.set_event_loop(None)
    gc.collect()


# [TEST FIX] Global absolute path for temporary test data.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TMP_TESTS_DIR = os.path.join(PROJECT_ROOT, ".tmp_tests")
os.makedirs(TMP_TESTS_DIR, exist_ok=True)

# [TEST FIX] Global injection is needed before any tests run to ensure Settings validation passes.
mcubridge.config.const.VOLATILE_STORAGE_PATHS = frozenset(
    list(mcubridge.config.const.VOLATILE_STORAGE_PATHS) + [TMP_TESTS_DIR, "/var/tmp", "/tmp"]
)


@pytest.fixture(autouse=True)
def isolate_test_paths() -> Iterator[None]:
    """Give each test unique file_system_root and mqtt_spool_dir to prevent cross-test interference.
    [SIL-2] FLASH PROTECTION: Always use /tmp (RAMFS) or verified .tmp_tests.
    """
    # Reset path cache to generate new paths for the current test case
    _test_paths["spool"] = None
    _test_paths["fs"] = None

    original_fs = mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT
    original_spool = mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR

    mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT = get_unique_test_fs()
    mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR = get_unique_test_spool()

    os.makedirs(mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT, exist_ok=True)
    os.makedirs(mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR, exist_ok=True)

    yield

    try:
        shutil.rmtree(mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT, ignore_errors=True)
        shutil.rmtree(mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR, ignore_errors=True)
    except Exception:
        pass

    mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT = original_fs
    mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR = original_spool


@pytest.fixture(autouse=True)
def reset_logging_handlers():
    """Close and remove all logging handlers after each test to prevent ResourceWarnings."""
    yield
    root = logging.getLogger()
    for handler in root.handlers[:]:
        try:
            handler.close()
        except (OSError, RuntimeError) as e:
            logging.debug("Logging handler close notice: %s", e)
        root.removeHandler(handler)


def _remove_persistent_test_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
        return

    try:
        path.unlink()
    except FileNotFoundError:
        return
    except IsADirectoryError:
        shutil.rmtree(path)


@pytest.fixture(autouse=True)
def isolate_persistent_runtime_paths() -> Iterator[None]:
    shared_paths = (
        Path(TMP_TESTS_DIR) / "yun_files/console",
        Path(TMP_TESTS_DIR) / "yun_files/mailbox_out",
        Path(TMP_TESTS_DIR) / "yun_files/mailbox_in",
        Path(TMP_TESTS_DIR) / "yun_files",
        Path(TMP_TESTS_DIR) / "mcubridge",
        Path(TMP_TESTS_DIR) / "mcubridge-tests-spool",
        Path(TMP_TESTS_DIR) / "spool_v3",
    )
    for path in shared_paths:
        _remove_persistent_test_path(path)
    yield
    for path in shared_paths:
        _remove_persistent_test_path(path)


@pytest.fixture(autouse=True)
def default_serial_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure load_runtime_config() sees a secure serial secret by default."""
    monkeypatch.setattr(
        settings,
        "get_uci_config",
        lambda: {
            **common.get_default_config(),
            "serial_shared_secret": "s_e_c_r_e_t_mock",
        },
    )


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
        mqtt_cafile=os.path.join(TMP_TESTS_DIR, "test-ca.pem"),
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_topic=protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        allowed_commands=(),
        file_system_root=get_unique_test_fs(),
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        mqtt_queue_limit=8,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        debug=False,
        console_queue_limit_bytes=64,
        mailbox_queue_limit=2,
        mailbox_queue_bytes_limit=32,
        serial_retry_timeout=0.05,
        serial_response_timeout=0.1,
        serial_retry_attempts=1,
        serial_shared_secret=b"s_e_c_r_e_t_mock",
        mqtt_spool_dir=get_unique_test_spool(),
        allow_non_tmp_paths=True,
    )


@pytest.fixture()
def runtime_state(runtime_config: RuntimeConfig) -> Iterator[RuntimeState]:
    """Provide a RuntimeState instance with proper cleanup."""
    state = create_runtime_state(runtime_config)
    state.mark_transport_connected()
    state.mark_synchronized()
    try:
        yield state
    finally:
        state.cleanup()


@pytest.fixture
def real_config():
    raw = settings.get_default_config()
    raw["serial_shared_secret"] = b"abcd1234"
    raw["serial_retry_timeout"] = 1.0
    raw["serial_response_timeout"] = 2.0
    raw["serial_handshake_fatal_failures"] = 15
    raw["process_max_concurrent"] = 4
    raw["allow_non_tmp_paths"] = True

    raw["mqtt_spool_dir"] = mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR
    raw["file_system_root"] = mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT

    config = msgspec.convert(raw, RuntimeConfig, strict=False)
    return config


@pytest.fixture
def service_stack(runtime_config: RuntimeConfig):
    """Provide a complete service stack (Service, State, Serial) for integration testing."""
    state = create_runtime_state(runtime_config)
    serial = SerialTransport(runtime_config, state, None)
    service = BridgeService(runtime_config, state, serial)
    serial.service = service
    try:
        yield service, state, serial
    finally:
        service.cleanup()
