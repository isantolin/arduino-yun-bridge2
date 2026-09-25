"""Pytest configuration for MCU Bridge tests."""

from __future__ import annotations

import asyncio
from asyncio import events as asyncio_events
from collections.abc import Iterator
import gc
import importlib.util
import inspect
import os
from pathlib import Path
import shutil
import sys
import time
from mcubridge.config.settings import load_runtime_config
from typing import Any, cast

from hypothesis import Phase, settings as hyp_settings, strategies as st
from hypothesis.strategies import DrawFn
import pytest
from pytest_mock import MockerFixture
import structlog
from mcubridge.config.logging import configure_logging, reset_handlers
from mcubridge.protocol import mcubridge_pb2 as pb

from mcubridge.config import common, settings
import mcubridge.config.common
import mcubridge.config.const
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_CLOUD_PORT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SAFE_BAUDRATE,
    DEFAULT_STATUS_INTERVAL,
)
import mcubridge.protocol.structures
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport

# Hypothesis profile registration for deterministic SIL-2 test runs
hyp_settings.register_profile(
    "dev",
    max_examples=30,
    derandomize=True,
    deadline=None,
    phases=(Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink),
)
hyp_settings.register_profile(
    "ci",
    max_examples=50,
    derandomize=True,
    deadline=None,
    phases=(Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink),
)
hyp_settings.register_profile(
    "fuzz",
    max_examples=200,
    derandomize=True,
    deadline=None,
    phases=(Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink),
)
hyp_settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))

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
# This patches the default directories for both direct RuntimeConfig(...)
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
original_get_default_config = mcubridge.config.common.get_default_config


class PatchedRuntimeConfig:
    def __new__(cls, *args: Any, **kwargs: Any) -> RuntimeConfig:
        mappings = {
            "cloud_topic": "topic_prefix",
            "cloud_spool_dir": "cloud_spool_dir",
            "cloud_queue_limit": "cloud_queue_limit",
            "cloud_enabled": "cloud_enabled",
            "cloud_host": "cloud_host",
            "cloud_port": "cloud_port",
            "cloud_user": "cloud_user",
            "cloud_pass": "cloud_pass",
            "cloud_tls": "cloud_tls",
            "cloud_cafile": "cloud_cafile",
            "cloud_certfile": "cloud_certfile",
            "cloud_keyfile": "cloud_keyfile",
        }
        for old, new in mappings.items():
            if old in kwargs:
                kwargs[new] = kwargs.pop(old)

        defaults = original_get_default_config()
        for k, v in defaults.items():
            if k not in kwargs:
                kwargs[k] = v
        default_spool = protocol.DEFAULT_CLOUD_SPOOL_DIR
        default_fs = protocol.DEFAULT_FILE_SYSTEM_ROOT
        if (
            "cloud_spool_dir" not in kwargs
            or kwargs["cloud_spool_dir"] == "/tmp/mcubridge/spool"
            or kwargs["cloud_spool_dir"] == default_spool
        ):
            kwargs["cloud_spool_dir"] = get_unique_test_spool()
        if (
            "file_system_root" not in kwargs
            or kwargs["file_system_root"] == "/tmp/mcubridge"
            or kwargs["file_system_root"] == default_fs
        ):
            kwargs["file_system_root"] = get_unique_test_fs()
        if isinstance(kwargs.get("serial_shared_secret"), str):
            kwargs["serial_shared_secret"] = kwargs["serial_shared_secret"].encode()
        return OriginalRuntimeConfig(*args, **kwargs)


def patched_get_default_config() -> dict[str, Any]:
    cfg = original_get_default_config()
    cfg["cloud_spool_dir"] = get_unique_test_spool()
    cfg["file_system_root"] = get_unique_test_fs()
    return cfg


setattr(mcubridge.protocol.structures, "RuntimeConfig", PatchedRuntimeConfig)
mcubridge.config.common.get_default_config = patched_get_default_config
# ==============================================================================


# Configure structlog centrally for test execution
configure_logging(debug=True, console=True)

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
            structlog.get_logger("mcubridge.tests").debug("Loop asyncgen shutdown notice", error=str(e))
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
    """Give each test unique file_system_root and cloud_spool_dir to prevent cross-test interference.
    [SIL-2] FLASH PROTECTION: Always use /tmp (RAMFS) or verified .tmp_tests.
    """
    # Reset path cache to generate new paths for the current test case
    _test_paths["spool"] = None
    _test_paths["fs"] = None

    original_fs = protocol.RUNTIME_CONFIG_DEFAULTS["file_system_root"]
    original_spool = protocol.RUNTIME_CONFIG_DEFAULTS["cloud_spool_dir"]

    unique_fs = get_unique_test_fs()
    unique_spool = get_unique_test_spool()

    protocol.RUNTIME_CONFIG_DEFAULTS["file_system_root"] = unique_fs
    protocol.RUNTIME_CONFIG_DEFAULTS["cloud_spool_dir"] = unique_spool

    os.makedirs(unique_fs, exist_ok=True)
    os.makedirs(unique_spool, exist_ok=True)

    yield

    try:
        if os.path.exists(unique_fs):
            shutil.rmtree(unique_fs)
        if os.path.exists(unique_spool):
            shutil.rmtree(unique_spool)
    except OSError as e:
        structlog.get_logger("mcubridge.tests").warning("Teardown path cleanup notice", error=str(e))

    protocol.RUNTIME_CONFIG_DEFAULTS["file_system_root"] = original_fs
    protocol.RUNTIME_CONFIG_DEFAULTS["cloud_spool_dir"] = original_spool


@pytest.fixture(autouse=True)
def reset_logging_handlers():
    """Close and remove all logging handlers after each test to prevent ResourceWarnings."""
    yield
    reset_handlers()


def _remove_persistent_test_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return

    try:
        os.unlink(path)
    except FileNotFoundError:
        structlog.get_logger("mcubridge.tests").debug("File not found during cleanup", path=str(path))
    except IsADirectoryError:
        shutil.rmtree(path, ignore_errors=True)
    except OSError as e:
        structlog.get_logger("mcubridge.tests").warning("Persistent path cleanup notice", path=str(path), error=str(e))


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
def default_serial_secret(mocker: MockerFixture) -> None:
    """Ensure load_runtime_config() sees a secure serial secret by default."""
    mocker.patch.object(
        settings,
        "get_uci_config",
        return_value={
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
        cloud_host="localhost",
        cloud_port=DEFAULT_CLOUD_PORT,
        cloud_user=None,
        cloud_pass=None,
        cloud_tls=True,
        cloud_cafile=os.path.join(TMP_TESTS_DIR, "test-ca.pem"),
        cloud_certfile=None,
        cloud_keyfile=None,
        topic_prefix=protocol.CLOUD_DEFAULT_TOPIC_PREFIX,
        allowed_commands=(),
        file_system_root=get_unique_test_fs(),
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        cloud_queue_limit=8,
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
        cloud_spool_dir=get_unique_test_spool(),
        allow_non_tmp_paths=True,
    )


@pytest.fixture()
def runtime_state(runtime_config: RuntimeConfig) -> Iterator[RuntimeState]:
    """Provide a RuntimeState instance with proper cleanup."""
    state = create_runtime_state(runtime_config)
    state.connection_fsm.connect()
    state.connection_fsm.synchronize()
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

    raw["cloud_spool_dir"] = protocol.DEFAULT_CLOUD_SPOOL_DIR
    raw["file_system_root"] = protocol.DEFAULT_FILE_SYSTEM_ROOT

    config = load_runtime_config(raw)
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
        state.cleanup()


@st.composite
def st_cloud_queued_publish(
    draw: DrawFn,
    min_payload_size: int = 0,
    max_payload_size: int = 1024,
) -> pb.CloudQueuedPublish:
    """Canonical Hypothesis strategy for generating valid Protobuf CloudQueuedPublish instances."""
    topic = draw(st.text(min_size=1, max_size=64))
    payload = draw(st.binary(min_size=min_payload_size, max_size=max_payload_size))
    qos = draw(st.integers(min_value=0, max_value=2))
    retain = draw(st.booleans())
    corr = draw(st.binary(max_size=32))
    reply = draw(st.text(max_size=64))
    return pb.CloudQueuedPublish(
        topic_name=topic,
        payload=payload,
        qos=qos,
        retain=retain,
        correlation_data=corr,
        response_topic=reply,
    )


@st.composite
def st_runtime_config(draw: DrawFn) -> RuntimeConfig:
    """Canonical Hypothesis strategy for generating valid RuntimeConfig instances."""
    baud = draw(st.sampled_from([9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]))
    port = draw(st.integers(min_value=1024, max_value=65535))
    prefix = draw(st.from_regex(r"^[a-z0-9_-]{1,16}$", fullmatch=True))
    secret = draw(st.binary(min_size=16, max_size=32))
    return RuntimeConfig(
        topic_prefix=prefix,
        serial_port="/dev/test",
        serial_baud=baud,
        cloud_port=port,
        serial_shared_secret=secret,
        allow_non_tmp_paths=True,
    )
