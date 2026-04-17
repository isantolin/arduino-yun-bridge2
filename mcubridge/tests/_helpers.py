"""Shared test helpers — importable from any test module."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

import msgspec

from mcubridge.config.common import get_default_config
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.structures import TopicRoute
from mcubridge.protocol.topics import Topic


def make_test_config(**overrides: object) -> RuntimeConfig:
    """Shared test config factory — avoids duplicated boilerplate across test modules."""
    raw = get_default_config()

    # [SIL-2] Ensure unique paths for every test instance to avoid SQLite race conditions
    tmp_root = tempfile.mkdtemp(prefix="mcubridge-test-")
    spool_dir = os.path.join(tmp_root, "spool")
    fs_root = os.path.join(tmp_root, "fs")
    os.makedirs(spool_dir, exist_ok=True)
    os.makedirs(fs_root, exist_ok=True)

    raw.update(
        serial_port="/dev/null",
        serial_shared_secret=b"s_e_c_r_e_t_mock",
        mqtt_spool_dir=spool_dir,
        file_system_root=fs_root,
    )
    raw.update(overrides)
    return msgspec.convert(raw, RuntimeConfig, strict=False)


def make_route(
    topic: Topic | str,
    *segments: str,
    prefix: str = "br",
) -> TopicRoute:
    """Build a TopicRoute for tests."""
    raw = f"{prefix}/{topic}/{'/'.join(segments)}"
    return TopicRoute(raw=raw, prefix=prefix, topic=topic, segments=tuple(segments))


def make_mqtt_msg(payload: bytes | str = b"") -> MagicMock:
    """Build a minimal MQTT Message mock for tests."""
    msg = MagicMock()
    msg.payload = payload.encode("utf-8") if isinstance(payload, str) else payload
    return msg
