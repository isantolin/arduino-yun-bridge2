from unittest.mock import AsyncMock, MagicMock
from typing import Any

import asyncio
import pytest
from cobs import cobs
from mcubridge.protocol import protocol
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.frame import build_frame
from mcubridge.protocol.protocol import Command
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    import os
    import time

    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    spool_dir = f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
        mqtt_topic="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123",
        file_system_root=fs_root,
        mqtt_spool_dir=spool_dir,
        allow_non_tmp_paths=True,
    )


def test_is_raw_binary_frame_valid_size() -> None:
    pass


@pytest.mark.asyncio
