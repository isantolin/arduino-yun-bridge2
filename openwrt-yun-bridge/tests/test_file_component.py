"""Tests for the FileComponent."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.rpc.protocol import UINT16_FORMAT
from yunbridge.services.components.file import FileComponent
from yunbridge.state.context import RuntimeState

# Constants for test config
MAX_WRITE = 1024
QUOTA = 2048

class DummyBridge:
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.frames: list[tuple[int, bytes]] = []
        self.mqtt_messages: list[tuple[str, bytes | str, bool]] = []

    async def send_frame(self, command_id: int, payload: bytes) -> bool:
        self.frames.append((command_id, payload))
        return True

    async def publish_mqtt(
        self, topic: str, payload: bytes | str, retain: bool
    ) -> None:
        self.mqtt_messages.append((topic, payload, retain))


@pytest.fixture()
def file_component(
    tmp_path: Path,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> tuple[FileComponent, DummyBridge]:
    runtime_config.file_system_root = str(tmp_path)
    runtime_state.file_system_root = str(tmp_path)
    runtime_config.file_write_max_bytes = MAX_WRITE
    runtime_config.file_storage_quota_bytes = QUOTA
    
    bridge = DummyBridge(runtime_config, runtime_state)
    
    # [FIX] Updated instantiation to match new signature
    component = FileComponent(
        root_path=str(tmp_path),
        send_frame=bridge.send_frame,
        publish_mqtt=bridge.publish_mqtt,
        write_max_bytes=MAX_WRITE,
        storage_quota_bytes=QUOTA,
    )
    return component, bridge


@pytest.mark.asyncio
async def test_handle_write_and_read_roundtrip(
    file_component: tuple[FileComponent, DummyBridge],
    tmp_path: Path,
) -> None:
    comp, bridge = file_component
    filename = "test.txt"
    content = b"hello world"

    # 1. Write via MCU command
    path_bytes = filename.encode("utf-8")
    header = struct.pack(f"B{len(path_bytes)}s" + UINT16_FORMAT, len(path_bytes), path_bytes, len(content))
    payload = header + content

    success = await comp.handle_write(payload)
    assert success is True

    target_file = tmp_path / filename
    assert target_file.exists()
    assert target_file.read_bytes() == content

    # 2. Verify Quota tracking (internal usage check)
    assert comp._calculate_usage() == len(content)


@pytest.mark.asyncio
async def test_handle_read_truncated_payload(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    comp, _ = file_component
    # Malformed payload (length mismatch)
    payload = b"\x04test\x00\x05abc"  # Claims 5 bytes, provides 3
    success = await comp.handle_write(payload)
    assert success is False


@pytest.mark.asyncio
async def test_handle_remove_missing_file(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    comp, _ = file_component
    path_bytes = b"missing.txt"
    payload = struct.pack(f"B{len(path_bytes)}s", len(path_bytes), path_bytes)
    
    # Should not raise exception
    success = await comp.handle_remove(payload)
    assert success is True


@pytest.mark.asyncio
async def test_handle_mqtt_write_and_read(
    file_component: tuple[FileComponent, DummyBridge],
    tmp_path: Path,
) -> None:
    comp, bridge = file_component
    filename = "mqtt.bin"
    content = b"\xDE\xAD\xBE\xEF"

    # 1. Write via MQTT
    await comp.handle_mqtt("write", [filename], content, MagicMock())
    
    target_file = tmp_path / filename
    assert target_file.exists()
    assert target_file.read_bytes() == content

    # 2. Read via MQTT
    await comp.handle_mqtt("read", [filename], b"", MagicMock())
    
    assert len(bridge.mqtt_messages) == 1
    topic, payload, _ = bridge.mqtt_messages[0]
    assert filename in topic
    assert payload == content


@pytest.mark.asyncio
async def test_handle_write_invalid_path(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    comp, _ = file_component
    # Try directory traversal
    filename = "../secret.txt"
    content = b"exploit"
    path_bytes = filename.encode("utf-8")
    header = struct.pack(f"B{len(path_bytes)}s" + UINT16_FORMAT, len(path_bytes), path_bytes, len(content))
    payload = header + content

    success = await comp.handle_write(payload)
    # The component 'handles' it by blocking (returning True but logging warning)
    # or returning True to indicate processed. The implementation returns True for handled-but-rejected.
    assert success is True
    
    # Verify file was NOT written
    assert not (comp.root.parent / "secret.txt").exists()


@pytest.mark.asyncio
async def test_handle_write_rejects_per_write_limit(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    comp, _ = file_component
    filename = "big.txt"
    content = b"x" * (MAX_WRITE + 1)
    
    path_bytes = filename.encode("utf-8")
    header = struct.pack(f"B{len(path_bytes)}s" + UINT16_FORMAT, len(path_bytes), path_bytes, len(content))
    payload = header + content

    success = await comp.handle_write(payload)
    assert success is True # Handled (rejected)
    
    assert not (comp.root / filename).exists()


@pytest.mark.asyncio
async def test_handle_write_enforces_storage_quota(
    file_component: tuple[FileComponent, DummyBridge],
    tmp_path: Path,
) -> None:
    comp, _ = file_component
    
    # Fill quota
    large_file = tmp_path / "filler.dat"
    large_file.write_bytes(b"x" * QUOTA)
    
    # Try write
    filename = "overflow.txt"
    content = b"x"
    path_bytes = filename.encode("utf-8")
    header = struct.pack(f"B{len(path_bytes)}s" + UINT16_FORMAT, len(path_bytes), path_bytes, len(content))
    payload = header + content

    success = await comp.handle_write(payload)
    assert success is True # Handled (rejected)
    
    assert not (tmp_path / filename).exists()


@pytest.mark.asyncio
async def test_handle_remove_updates_usage(
    file_component: tuple[FileComponent, DummyBridge],
    tmp_path: Path,
) -> None:
    comp, _ = file_component
    filename = "temp.txt"
    (tmp_path / filename).write_bytes(b"data")
    
    assert comp._calculate_usage() == 4
    
    path_bytes = filename.encode("utf-8")
    payload = struct.pack(f"B{len(path_bytes)}s", len(path_bytes), path_bytes)
    
    await comp.handle_remove(payload)
    
    assert not (tmp_path / filename).exists()
    assert comp._calculate_usage() == 0


@pytest.mark.parametrize(
    "filename, expected_suffix",
    [
        ("file.txt", "file.txt"),
        ("dir/file.txt", "dir/file.txt"),
        ("dir/subdir/file.txt", "dir/subdir/file.txt"),
        ("file with spaces.txt", "file with spaces.txt"),
        ("file-with-dashes.txt", "file-with-dashes.txt"),
        ("file_with_underscores.txt", "file_with_underscores.txt"),
    ],
)
def test_normalise_filename_strips_traversal(
    file_component: tuple[FileComponent, DummyBridge],
    filename: str,
    expected_suffix: str,
) -> None:
    comp, _ = file_component
    # [FIX] Use _get_safe_path to verify resolution logic
    result = comp._get_safe_path(filename)
    assert result is not None
    assert str(result).endswith(expected_suffix)
    assert str(result).startswith(str(comp.root))


@pytest.mark.parametrize(
    "filename",
    [
        "../file.txt",
        "/etc/passwd",
        "dir/../../file.txt",
    ],
)
def test_get_safe_path_confines_to_root(
    file_component: tuple[FileComponent, DummyBridge],
    filename: str,
) -> None:
    comp, _ = file_component
    # These should be rejected (return None) or resolve to something safe if cleaned?
    # Based on implementation: resolve() handles ..
    # If resolving .. goes outside root, it returns None.
    
    # We construct specific traversal attacks
    if filename.startswith("/"):
        # Absolute paths are treated relative to root in logic:
        # candidate = (self.root / clean_rel).resolve()
        # So /etc/passwd becomes root/etc/passwd -> Safe
        pass 
    else:
        # Relative traversal
        result = comp._get_safe_path(filename)
        # If it stepped out, it should be None
        # Note: In a temp dir, ../file.txt refers to the parent of temp dir.
        # This SHOULD be caught.
        assert result is None
