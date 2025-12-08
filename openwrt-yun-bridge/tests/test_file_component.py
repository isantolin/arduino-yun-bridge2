"""Tests for FileComponent MCU/MQTT behaviour."""
from __future__ import annotations

import asyncio
import string
from pathlib import Path
from typing import Any
from collections.abc import Coroutine

import pytest
from aiomqtt.client import Message as MQTTMessage
from hypothesis import HealthCheck, given, settings, strategies as st

from yunbridge.config.settings import RuntimeConfig
from yunbridge.mqtt.messages import QueuedPublish
from yunbridge.rpc.protocol import Command, Status
from yunbridge.services.components.base import BridgeContext
from yunbridge.services.components.file import FileComponent
from yunbridge.state.context import RuntimeState


class DummyBridge(BridgeContext):
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.sent_frames: list[tuple[int, bytes]] = []
        self.published: list[QueuedPublish] = []

    async def send_frame(self, command_id: int, payload: bytes = b"") -> bool:
        self.sent_frames.append((command_id, payload))
        return True

    async def enqueue_mqtt(
        self,
        message: QueuedPublish,
        *,
        reply_context: MQTTMessage | None = None,
    ) -> None:
        self.published.append(message)

    def is_command_allowed(self, command: str) -> bool:
        return True

    async def schedule_background(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:  # pragma: no cover
        return asyncio.create_task(coroutine, name=name)


@pytest.fixture()
def file_component(
    tmp_path: Path,
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> tuple[FileComponent, DummyBridge]:
    runtime_config.file_system_root = str(tmp_path)
    runtime_state.file_system_root = str(tmp_path)
    bridge = DummyBridge(runtime_config, runtime_state)
    component = FileComponent(runtime_config, runtime_state, bridge)
    return component, bridge


@pytest.mark.asyncio
async def test_handle_write_and_read_roundtrip(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    payload = bytes([3]) + b"foo" + (4).to_bytes(2, "big") + b"data"
    await component.handle_write(payload)

    read_payload = bytes([3]) + b"foo"
    await component.handle_read(read_payload)

    assert bridge.sent_frames[-1][0] == Command.CMD_FILE_READ_RESP.value
    assert bridge.sent_frames[-1][1] == b"\x00\x04data"


@pytest.mark.asyncio
async def test_handle_read_truncated_payload(
    file_component: tuple[FileComponent, DummyBridge],
    tmp_path: Path,
) -> None:
    component, bridge = file_component
    (tmp_path / "long.bin").write_bytes(b"x" * 512)
    payload = bytes([8]) + b"long.bin"
    await component.handle_read(payload)

    assert bridge.sent_frames[-1][0] == Command.CMD_FILE_READ_RESP.value
    assert bridge.sent_frames[-1][1].startswith(b"\x00\xfe")


@pytest.mark.asyncio
async def test_handle_remove_missing_file(
    file_component: tuple[FileComponent, DummyBridge]
) -> None:
    component, bridge = file_component
    payload = bytes([7]) + b"missing"
    await component.handle_remove(payload)

    assert bridge.sent_frames[-1][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_handle_mqtt_write_and_read(
    file_component: tuple[FileComponent, DummyBridge],
    tmp_path: Path,
) -> None:
    component, bridge = file_component
    await component.handle_mqtt(
        "write",
        ["dir", "file.txt"],
        b"payload",
    )
    assert (tmp_path / "dir" / "file.txt").read_bytes() == b"payload"

    await component.handle_mqtt(
        "read",
        ["dir", "file.txt"],
        b"",
    )

    assert bridge.published
    assert bridge.published[-1].payload == b"payload"


@pytest.mark.asyncio
async def test_handle_write_invalid_path(
    file_component: tuple[FileComponent, DummyBridge]
) -> None:
    component, bridge = file_component
    payload = bytes([2]) + b".." + (1).to_bytes(2, "big") + b"x"
    await component.handle_write(payload)

    assert bridge.sent_frames[-1][0] == Status.ERROR.value


SAFE_FILENAME_CHARS = (
    string.ascii_letters + string.digits + "/._- " + "\\"
)
FILENAME_INPUTS = st.text(SAFE_FILENAME_CHARS, min_size=0, max_size=64)


@given(filename=FILENAME_INPUTS)
def test_normalise_filename_strips_traversal(filename: str) -> None:
    result = FileComponent._normalise_filename(filename)
    if result is None:
        return
    assert not result.is_absolute()
    for part in result.parts:
        assert part not in {"", ".", ".."}
        assert "\x00" not in part


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(filename=FILENAME_INPUTS)
def test_get_safe_path_confines_to_root(
    file_component: tuple[FileComponent, DummyBridge], filename: str
) -> None:
    component, _ = file_component
    base_dir = Path(component.state.file_system_root).expanduser().resolve()
    safe_path = component._get_safe_path(filename)
    if safe_path is None:
        return
    assert safe_path.is_relative_to(base_dir)
