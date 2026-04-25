import os
import tempfile
import msgspec
from typing import Any
from mcubridge.config.settings import RuntimeConfig, get_default_config
from mcubridge.protocol.topics import Topic, TopicRoute

def make_test_config(**overrides: object) -> RuntimeConfig:
    """Shared test config factory — avoids duplicated boilerplate across test modules."""
    raw = get_default_config()

    # [SIL-2] Ensure unique paths for every test instance to avoid SQLite race conditions
    # FLASH PROTECTION: Must be in /tmp (RAMFS)
    tmp_root = tempfile.mkdtemp(prefix="mcubridge-test-", dir=".tmp_tests")
    spool_dir = os.path.join(tmp_root, "spool")
    fs_root = os.path.join(tmp_root, "fs")
    os.makedirs(spool_dir, exist_ok=True)
    os.makedirs(fs_root, exist_ok=True)

    raw.update(
        serial_port="/dev/null",
        serial_shared_secret=b"s_e_c_r_e_t_mock",
        mqtt_spool_dir=spool_dir,
        file_system_root=fs_root,
        allow_non_tmp_paths=True,
    )
    raw.update(overrides)
    return msgspec.convert(raw, RuntimeConfig, strict=False)

def make_route(
    topic: Topic | str,
    identifier: str,
    remainder: tuple[str, ...] = (),
) -> TopicRoute:
    """Helper to create a TopicRoute for testing."""
    return TopicRoute(
        topic=Topic(topic) if isinstance(topic, str) else topic,
        identifier=identifier,
        remainder=remainder,
    )
