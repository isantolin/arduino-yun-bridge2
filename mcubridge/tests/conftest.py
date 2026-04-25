import sys
import asyncio
import os
import tempfile
from typing import Any
from unittest.mock import MagicMock, AsyncMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    import uci
except ImportError:
    _stubs_path = os.path.join(_project_root, "typings", "stubs")
    if _stubs_path not in sys.path:
        sys.path.insert(0, _stubs_path)
    import uci
    sys.modules["uci"] = uci

if "serial_asyncio_fast" not in sys.modules:
    mock_saf = MagicMock()
    mock_saf.open_serial_connection = AsyncMock(
        return_value=(AsyncMock(spec=asyncio.StreamReader), AsyncMock(spec=asyncio.StreamWriter))
    )
    mock_saf.create_serial_connection = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    sys.modules["serial_asyncio_fast"] = mock_saf

from mcubridge.config import common
import mcubridge.config.logging
from mcubridge.config import settings

mcubridge.config.logging.USE_SYSLOG = False
settings.USE_SYSLOG = False

import pytest
import msgspec
from mcubridge.config.settings import RuntimeConfig, get_default_config
from mcubridge.state.context import create_runtime_state
from mcubridge.protocol.topics import Topic
from mcubridge.protocol.structures import TopicRoute
from aiomqtt.message import Message

def make_test_config(**overrides: Any) -> RuntimeConfig:
    raw = get_default_config()
    tmp_root = tempfile.mkdtemp(prefix="mcubridge-test-", dir=".tmp_tests")
    raw.update({
        "serial_port": "/dev/null",
        "mqtt_spool_dir": os.path.join(tmp_root, "spool"),
        "file_system_root": os.path.join(tmp_root, "fs"),
        "allow_non_tmp_paths": True,
    })
    raw.update(overrides)
    return msgspec.convert(raw, RuntimeConfig, strict=False)

@pytest.fixture
def runtime_config() -> RuntimeConfig:
    return make_test_config()

@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig):
    state = create_runtime_state(runtime_config)
    yield state
    state.cleanup()

def make_component_container(state: Any = None, config: Any = None, **components: Any):
    from mcubridge.services.runtime import BridgeService
    from mcubridge.mqtt.spool_manager import MqttSpoolManager
    cfg = config or make_test_config()
    st = state or create_runtime_state(cfg)
    spool = MagicMock(spec=MqttSpoolManager)
    service = BridgeService(cfg, st, spool)
    # The dispatcher already registered components in __init__ (manual)
    # but we can replace them in the container if provided as mocks
    for name, comp in components.items():
        from mcubridge.services import (
            ConsoleComponent, DatastoreComponent, FileComponent,
            MailboxComponent, PinComponent, ProcessComponent,
            SpiComponent, SystemComponent
        )
        mapping = {
            "console": ConsoleComponent, "datastore": DatastoreComponent,
            "file": FileComponent, "mailbox": MailboxComponent,
            "pin": PinComponent, "process": ProcessComponent,
            "spi": SpiComponent, "system": SystemComponent,
        }
        if name in mapping:
            service._container.forget_factory(mapping[name])
            service._container.register_value(mapping[name], comp)
    return service._container
