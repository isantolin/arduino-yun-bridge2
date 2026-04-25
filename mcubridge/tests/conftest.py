import sys
import asyncio
import os
import time
from unittest.mock import MagicMock, AsyncMock

# Add current dir to path for imports
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# [TEST FIX] Ensure 'uci' stub is available globally before any module imports it.
try:
    import uci
except ImportError:
    _stubs_path = os.path.join(_project_root, "typings", "stubs")
    if _stubs_path not in sys.path:
        sys.path.insert(0, _stubs_path)
    import uci  # This imports from mcubridge/stubs/uci/

    sys.modules["uci"] = uci


# [TEST FIX] Mock 'pyserial-asyncio-fast' as it is a compiled extension not available in dev env.
if "serial_asyncio_fast" not in sys.modules:
    mock_saf = MagicMock()
    # Mock open_serial_connection to return (StreamReader, StreamWriter)
    mock_saf.open_serial_connection = AsyncMock(
        return_value=(
            AsyncMock(spec=asyncio.StreamReader),
            AsyncMock(spec=asyncio.StreamWriter),
        )
    )
    # Maintain create_serial_connection for older tests that haven't been migrated yet
    mock_saf.create_serial_connection = AsyncMock(
        return_value=(AsyncMock(), AsyncMock())
    )
    sys.modules["serial_asyncio_fast"] = mock_saf


# [TEST FIX] Disable SysLog for all tests to prevent unclosed UNIX sockets (ResourceWarning)
# and interference with Python 3.13 representation during cleanup.
from mcubridge.config import common
import mcubridge.config.logging
from mcubridge.config import settings

# Force no syslog in tests
mcubridge.config.logging.USE_SYSLOG = False
settings.USE_SYSLOG = False

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.state.context import create_runtime_state


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    """Provides a fresh RuntimeConfig for each test."""
    from tests._helpers import make_test_config
    return make_test_config(
        serial_port="/dev/null",
        serial_baud=115200,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_topic="br",
        status_interval=60.0,
    )


@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig):
    """Provides a fresh RuntimeState for each test, ensuring cleanup."""
    state = create_runtime_state(runtime_config)
    yield state
    state.cleanup()
