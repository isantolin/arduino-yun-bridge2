"""Pytest configuration for MCU Bridge tests."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import svcs
import structlog

import pytest

# [TEST FIX] Mock 'uci' module strictly before importing mcubridge.common.
# This simulates the OpenWrt environment where 'uci' is available.
# We use the stub from stubs/uci/ which provides proper UciException and Uci classes.
if "uci" not in sys.modules:
    # Add stubs to path and import the real stub
    _stubs_path = str(Path(__file__).parent.parent / "stubs")
    if _stubs_path not in sys.path:
        sys.path.insert(0, _stubs_path)
    import uci  # This imports from mcubridge/stubs/uci/

    sys.modules["uci"] = uci


# [TEST FIX] Mock 'pyserial-asyncio-fast' as it is a compiled extension not available in dev env.
if "serial_asyncio_fast" not in sys.modules:
    from unittest.mock import AsyncMock

    mock_saf = MagicMock()
    # Mock open_serial_connection to return (StreamReader, StreamWriter)
    mock_saf.open_serial_connection = AsyncMock(
        return_value=(AsyncMock(spec=asyncio.StreamReader), AsyncMock(spec=asyncio.StreamWriter))
    )
    # Maintain create_serial_connection for older tests that haven't been migrated yet
    mock_saf.create_serial_connection = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    sys.modules["serial_asyncio_fast"] = mock_saf


# [TEST FIX] Disable SysLog for all tests to prevent unclosed UNIX sockets (ResourceWarning)
# and interference with Python 3.13 representation during cleanup.
from mcubridge.config import common
from mcubridge.config import logging as mcubridge_logging
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

mcubridge_logging.SYSLOG_SOCKET = Path("/dev/null/no-syslog-in-tests")

# [TEST FIX] Configure structlog to use stdlib backend so pytest's caplog works.
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
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
        warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*get_event_loop_policy.*")
        policy = asyncio.get_event_loop_policy()
    loop = getattr(getattr(policy, "_local", None), "_loop", None)
    if loop is not None and not loop.is_closed():
        loop.close()
    asyncio.set_event_loop(None)
    # Collect garbage while temporarily suppressing PytestUnraisableExceptionWarning
    # caused by diskcache sqlite3.Connection objects finalised during GC.  The
    # connections are managed by diskcache internals and cannot be closed earlier
    # without coupling test infrastructure to the library's threading model.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=pytest.PytestUnraisableExceptionWarning)
        gc.collect()


@pytest.fixture(autouse=True)
def _isolate_file_system_root(tmp_path: Path) -> Iterator[None]:  # type: ignore[reportUnusedFunction]
    """Give each test a unique file_system_root to prevent cross-test interference."""
    import mcubridge.config.const as _const

    original = _const.DEFAULT_FILE_SYSTEM_ROOT
    _const.DEFAULT_FILE_SYSTEM_ROOT = str(tmp_path / "yun_files")
    yield
    _const.DEFAULT_FILE_SYSTEM_ROOT = original


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
        Path("/tmp/yun_files/console"),
        Path("/tmp/yun_files/mailbox_out"),
        Path("/tmp/yun_files/mailbox_in"),
        Path("/tmp/yun_files"),
        Path("/tmp/mcubridge"),
        Path("/tmp/mcubridge-tests-spool"),
        Path("/tmp/spool_v3"),
    )
    for path in shared_paths:
        _remove_persistent_test_path(path)
    yield
    for path in shared_paths:
        _remove_persistent_test_path(path)


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
                original_handlers.append((handler, handler.level))  # type: ignore[reportUnknownMemberType]
                handler.level = logging.NOTSET

    yield

    # Restore (though usually not necessary for tests)
    for handler, level in original_handlers:  # type: ignore[reportUnknownVariableType]
        handler.level = level


@pytest.fixture(autouse=True)
def _default_serial_secret(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[reportUnusedFunction]
    """Ensure load_runtime_config() sees a secure serial secret by default.

    Settings are UCI-only, so we inject a deterministic UCI payload for tests.
    """

    monkeypatch.setattr(
        settings, "get_uci_config", lambda: {**common.get_default_config(), "serial_shared_secret": "s_e_c_r_e_t_mock"}
    )


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


def make_component_container(
    *,
    console: object = None,
    datastore: object = None,
    file: object = None,
    mailbox: object = None,
    pin: object = None,
    process: object = None,
    spi: object = None,
    system: object = None,
) -> "svcs.Container":
    """Build a ``svcs.Container`` pre-loaded with component instances (or mocks).

    MagicMock objects have auto-generated ``__aenter__``/``__aexit__`` which
    makes ``svcs.Container.get()`` wrongly detect them as async context
    managers.  We strip those attributes so ``get()`` works synchronously.
    """
    from mcubridge.services import (
        ConsoleComponent,
        DatastoreComponent,
        FileComponent,
        MailboxComponent,
        PinComponent,
        ProcessComponent,
        SpiComponent,
        SystemComponent,
    )

    registry = svcs.Registry()
    for svc_type, inst in (
        (ConsoleComponent, console),
        (DatastoreComponent, datastore),
        (FileComponent, file),
        (MailboxComponent, mailbox),
        (PinComponent, pin),
        (ProcessComponent, process),
        (SpiComponent, spi),
        (SystemComponent, system),
    ):
        if inst is not None:
            # Prevent svcs from detecting MagicMock as an async CM.
            if isinstance(inst, MagicMock):
                for attr in ("__aenter__", "__aexit__"):
                    try:
                        delattr(inst, attr)
                    except AttributeError:
                        pass
            registry.register_value(svc_type, inst)  # type: ignore[reportUnknownMemberType]
    return svcs.Container(registry)


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
        mqtt_spool_dir=f"/tmp/mcubridge-tests-spool-{os.getpid()}",
    )


@pytest.fixture()
def runtime_state(runtime_config: RuntimeConfig) -> Iterator[RuntimeState]:
    """Provide a RuntimeState instance with proper cleanup."""
    state = create_runtime_state(runtime_config, initialize_spool=True)
    state.mark_transport_connected()
    state.mark_synchronized()
    try:
        yield state
    finally:
        state.cleanup()


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
