"""Pytest configuration for MCU Bridge tests."""

from __future__ import annotations
import msgspec

import asyncio
import importlib.util
import inspect
import logging
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import structlog
import mcubridge.config.const

import pytest

from mcubridge.config import common
from mcubridge.config import settings
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

# [TEST FIX] We use the stub from stubs/uci/ which provides proper UciException and Uci classes.
# The stub is placed in sys.path so 'import uci' will succeed naturally without sys.modules hack.
_stubs_path = str(Path(__file__).parent.parent / "stubs")
if _stubs_path not in sys.path:
    sys.path.insert(0, _stubs_path)


# No longer injecting serial_asyncio_fast into sys.modules.
# Tests MUST mock mcubridge.transport.serial components instead.

# [TEST FIX] Disable SysLog for all tests to prevent unclosed UNIX sockets (ResourceWarning)
# and interference with Python 3.13 representation during cleanup.

# [TEST FIX] Configure structlog purely natively but route to logging for caplog compatibility.
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
    cache_logger_on_first_use=False,
)

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
    if isinstance(policy, asyncio.AbstractEventLoopPolicy):  # type: ignore[reportGeneralTypeIssues]
        asyncio.set_event_loop_policy(policy)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        kwargs = {
            name: pyfuncitem.funcargs[name]
            for name in pyfuncitem._fixtureinfo.argnames  # type: ignore[reportPrivateUsage]
        }
        loop.run_until_complete(test_function(**kwargs))
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except (RuntimeError, ValueError):
            pass
        loop.close()
        asyncio.set_event_loop(None)
    return True


@pytest.fixture(autouse=True)
def force_gc_cleanup():
    """Ensure all resources are released after each test to reach zero warnings."""
    import gc
    import warnings

    yield
    # Close any stale event loop left by asyncio.run() or explicit set_event_loop
    # to prevent ResourceWarning from leaked self-pipe sockets across tests.
    #
    # Python 3.13 deprecated get_event_loop() when no current loop exists.
    # Access the policy's thread-local directly to avoid triggering the
    # DeprecationWarning that filterwarnings=["error"] would promote to fatal.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", category=DeprecationWarning, message=".*get_event_loop_policy.*"
        )
        policy = asyncio.get_event_loop_policy()
    loop = getattr(getattr(policy, "_local", None), "_loop", None)
    if loop is not None and not loop.is_closed():
        loop.close()
    asyncio.set_event_loop(None)
    # Collect garbage to finalize any objects that hold OS resources.
    # The diskcache ResourceWarning was fixed at the source (RuntimeState.__del__
    # + cleanup() resets mailbox queues to plain deques), so no suppression needed.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", category=pytest.PytestUnraisableExceptionWarning
        )
        gc.collect()


# [TEST FIX] Global absolute path for temporary test data.
# This ensures all tests use the same base directory and avoids 'Disk quota exceeded'
# on restricted environments by allowing the user to redirect it.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TMP_TESTS_DIR = os.path.join(PROJECT_ROOT, ".tmp_tests")
os.makedirs(TMP_TESTS_DIR, exist_ok=True)

# [TEST FIX] Global injection is needed before any tests run to ensure Settings validation passes.
mcubridge.config.const.VOLATILE_STORAGE_PATHS = frozenset(
    list(mcubridge.config.const.VOLATILE_STORAGE_PATHS) + [TMP_TESTS_DIR, "/var/tmp"]
)


@pytest.fixture(autouse=True)
def _isolate_test_paths() -> Iterator[None]:  # type: ignore[reportUnusedFunction]
    """Give each test unique file_system_root and mqtt_spool_dir to prevent cross-test interference.
    [SIL-2] FLASH PROTECTION: Always use /tmp (RAMFS) or verified .tmp_tests.
    """
    import mcubridge.config.const
    import tempfile

    original_fs = mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT
    original_spool = mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR

    tmp_base = tempfile.mkdtemp(prefix="mcubridge-pytest-", dir=TMP_TESTS_DIR)
    mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT = str(Path(tmp_base) / "yun_files")
    mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR = str(Path(tmp_base) / "spool")
    yield
    mcubridge.config.const.DEFAULT_FILE_SYSTEM_ROOT = original_fs
    mcubridge.config.const.DEFAULT_MQTT_SPOOL_DIR = original_spool
    shutil.rmtree(tmp_base, ignore_errors=True)


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


def _remove_persistent_test_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return

    try:
        path.unlink()
    except FileNotFoundError:
        return
    except IsADirectoryError:
        shutil.rmtree(path, ignore_errors=True)


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
def _default_serial_secret(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[reportUnusedFunction]
    """Ensure load_runtime_config() sees a secure serial secret by default.

    Settings are UCI-only, so we inject a deterministic UCI payload for tests.
    """

    monkeypatch.setattr(
        settings,
        "get_uci_config",
        lambda: {
            **common.get_default_config(),
            "serial_shared_secret": "s_e_c_r_e_t_mock",
        },
    )


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


@pytest.fixture()
def runtime_config() -> RuntimeConfig:
    import time

    # [TEST FIX] Ensure each test worker has its own unique FS root to avoid SQLite locking
    unique_root = os.path.join(
        TMP_TESTS_DIR, f"mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    )
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
        file_system_root=unique_root,
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
        mqtt_spool_dir=os.path.join(
            TMP_TESTS_DIR, f"mcubridge-test-spool-{os.getpid()}"
        ),
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
    from mcubridge.config import settings

    raw = settings.get_default_config()
    raw["serial_shared_secret"] = b"abcd1234"
    raw["serial_retry_timeout"] = 1.0
    raw["serial_response_timeout"] = 2.0
    raw["serial_handshake_fatal_failures"] = 15
    raw["process_max_concurrent"] = 4
    raw["allow_non_tmp_paths"] = True
    config = msgspec.convert(raw, settings.RuntimeConfig, strict=False)
    return config
