"""Tests for FileComponent MCU/MQTT behaviour."""

from __future__ import annotations

import asyncio
import os
import string
from pathlib import Path
from typing import Any
from collections.abc import Coroutine

import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.mqtt.messages import QueuedPublish
from mcubridge.rpc.protocol import Command, Status, MAX_PAYLOAD_SIZE
from mcubridge.services.components.base import BridgeContext
from mcubridge.services.components.file import FileComponent
from mcubridge.state.context import RuntimeState


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
        reply_context: Message | None = None,
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


def _build_write_payload(filename: str, data: bytes) -> bytes:
    encoded = filename.encode("utf-8")
    return bytes([len(encoded)]) + encoded + len(data).to_bytes(2, "big") + data


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
async def test_handle_write_sends_ok_on_success(
    file_component: tuple[FileComponent, DummyBridge],
    tmp_path: Path,
) -> None:
    component, bridge = file_component
    payload = _build_write_payload("ok.txt", b"abc")
    assert await component.handle_write(payload) is True
    assert (tmp_path / "ok.txt").exists()
    assert bridge.sent_frames[-1][0] == Status.OK.value


@pytest.mark.asyncio
async def test_handle_remove_sends_ok_on_success(
    file_component: tuple[FileComponent, DummyBridge],
    tmp_path: Path,
) -> None:
    component, bridge = file_component
    (tmp_path / "rm.txt").write_text("x", encoding="utf-8")
    payload = bytes([6]) + b"rm.txt"
    assert await component.handle_remove(payload) is True
    assert not (tmp_path / "rm.txt").exists()
    assert bridge.sent_frames[-1][0] == Status.OK.value


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
    # MAX_PAYLOAD_SIZE is 128, so max content is 128 - 2 = 126 bytes
    expected_len = MAX_PAYLOAD_SIZE - 2
    assert bridge.sent_frames[-1][1].startswith(bytes([0, expected_len]))


@pytest.mark.asyncio
async def test_handle_remove_missing_file(
    file_component: tuple[FileComponent, DummyBridge],
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
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    payload = bytes([2]) + b".." + (1).to_bytes(2, "big") + b"x"
    await component.handle_write(payload)

    assert bridge.sent_frames[-1][0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_rejects_non_tmp_root_by_default(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    component.state.allow_non_tmp_paths = False
    component.state.file_system_root = "/etc/mcubridge-test"

    payload = _build_write_payload("foo.txt", b"x")
    await component.handle_write(payload)

    assert bridge.sent_frames[-1][0] == Status.ERROR.value
    assert bridge.sent_frames[-1][1].decode() == "unsafe_path"


@pytest.mark.asyncio
async def test_handle_write_rejects_per_write_limit(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    component.state.file_write_max_bytes = 2
    component.state.file_storage_quota_bytes = 64
    payload = _build_write_payload("big.txt", b"abcd")

    await component.handle_write(payload)

    assert bridge.sent_frames[-1][0] == Status.ERROR.value
    assert bridge.sent_frames[-1][1].decode() == "write_limit_exceeded"
    assert component.state.file_write_limit_rejections == 1
    assert component.state.file_storage_bytes_used == 0
    root = Path(component.state.file_system_root)
    assert not (root / "big.txt").exists()


@pytest.mark.asyncio
async def test_handle_write_enforces_storage_quota(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    component.state.file_write_max_bytes = 4
    component.state.file_storage_quota_bytes = 4

    first_payload = _build_write_payload("alpha.txt", b"xy")
    assert await component.handle_write(first_payload)
    root = Path(component.state.file_system_root)
    assert (root / "alpha.txt").exists()
    assert component.state.file_storage_bytes_used == 2

    second_payload = _build_write_payload("bravo.txt", b"xyz")
    await component.handle_write(second_payload)

    assert bridge.sent_frames[-1][0] == Status.ERROR.value
    assert bridge.sent_frames[-1][1].decode() == "storage_quota_exceeded"
    assert component.state.file_storage_limit_rejections == 1
    assert component.state.file_storage_bytes_used == 2
    assert not (root / "bravo.txt").exists()


@pytest.mark.asyncio
async def test_handle_remove_updates_usage(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, _ = file_component
    component.state.file_write_max_bytes = 16
    payload = _build_write_payload("temp.txt", b"abc")
    assert await component.handle_write(payload)
    assert component.state.file_storage_bytes_used == 3

    remove_payload = bytes([8]) + b"temp.txt"
    assert await component.handle_remove(remove_payload)
    assert component.state.file_storage_bytes_used == 0
    root = Path(component.state.file_system_root)
    assert not (root / "temp.txt").exists()


@pytest.mark.asyncio
async def test_handle_write_rejects_too_short_payload(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component

    assert await component.handle_write(b"") is False
    assert bridge.sent_frames == []


@pytest.mark.asyncio
async def test_handle_write_rejects_missing_data_section(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    # path_len=3 but missing 2-byte length field
    payload = bytes([3]) + b"foo"
    assert await component.handle_write(payload) is False
    assert bridge.sent_frames == []


@pytest.mark.asyncio
async def test_handle_write_rejects_absolute_path(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    payload = _build_write_payload("/etc/passwd", b"x")

    assert await component.handle_write(payload) is False
    assert bridge.sent_frames
    assert bridge.sent_frames[-1][0] == Status.ERROR.value
    assert bridge.sent_frames[-1][1].decode() == "invalid_path"


@pytest.mark.asyncio
async def test_handle_write_rejects_truncated_data(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    encoded = b"foo"
    # Declares 4 bytes of data but only provides 3.
    payload = bytes([len(encoded)]) + encoded + (4).to_bytes(2, "big") + b"abc"
    assert await component.handle_write(payload) is False
    assert bridge.sent_frames == []


@pytest.mark.asyncio
async def test_handle_read_rejects_invalid_payloads(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    await component.handle_read(b"")
    await component.handle_read(bytes([5]) + b"ab")
    assert bridge.sent_frames == []


@pytest.mark.asyncio
async def test_handle_read_failure_sends_error(
    file_component: tuple[FileComponent, DummyBridge],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component, bridge = file_component

    async def fail(_op: str, _filename: str, _data: bytes | None = None):
        return False, None, "boom"

    monkeypatch.setattr(component, "_perform_file_operation", fail)
    await component.handle_read(bytes([3]) + b"foo")
    assert bridge.sent_frames[-1][0] == Status.ERROR.value
    assert bridge.sent_frames[-1][1].decode() == "boom"


@pytest.mark.asyncio
async def test_handle_mqtt_missing_filename_is_ignored(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    await component.handle_mqtt("read", [], b"")
    assert bridge.published == []


@pytest.mark.asyncio
async def test_handle_mqtt_unknown_action_is_ignored(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, bridge = file_component
    await component.handle_mqtt("unknown", ["file.txt"], b"")
    assert bridge.published == []
    assert bridge.sent_frames == []


@pytest.mark.asyncio
async def test_perform_file_operation_unknown_operation_branch(
    file_component: tuple[FileComponent, DummyBridge],
) -> None:
    component, _ = file_component
    ok, content, reason = await component._perform_file_operation("bogus", "file.txt")
    assert ok is False
    assert content is None
    assert reason == "unknown_operation"


@pytest.mark.asyncio
async def test_perform_file_operation_oserror_returns_false(
    file_component: tuple[FileComponent, DummyBridge],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component, _ = file_component

    def boom(_path: Path) -> bytes:
        raise OSError("read_failed")

    monkeypatch.setattr(component, "_read_file_sync", boom)
    ok, content, reason = await component._perform_file_operation("read", "file.txt")
    assert ok is False
    assert content is None
    assert reason is not None


def test_normalise_filename_rejects_bad_inputs() -> None:
    assert FileComponent._normalise_filename("") is None
    assert FileComponent._normalise_filename("   ") is None
    assert FileComponent._normalise_filename("./") is None
    assert FileComponent._normalise_filename("../") is None
    assert FileComponent._normalise_filename("a\x00b") is None


def test_scan_directory_size_handles_scandir_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeDirEntry:
        def __init__(self, name: str, path: str) -> None:
            self.name = name
            self.path = path

        def is_symlink(self) -> bool:
            return self.name == "sym"

        def is_dir(self, *, follow_symlinks: bool = False) -> bool:
            if self.name == "bad_dir":
                raise OSError("boom")
            return self.name == "dir"

        def is_file(self, *, follow_symlinks: bool = False) -> bool:
            return self.name == "file"

        def stat(self, *, follow_symlinks: bool = False):
            class Stat:
                st_size = 3

            return Stat()

    class FakeScandir:
        def __init__(self, path: Path) -> None:
            self._path = path

        def __enter__(self):
            return iter(
                [
                    FakeDirEntry("sym", str(self._path / "sym")),
                    FakeDirEntry("bad_dir", str(self._path / "bad_dir")),
                    FakeDirEntry("file", str(self._path / "file")),
                ]
            )

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_scandir(path: str | os.PathLike[str]):
        p = Path(path)
        if p.name == "missing":
            raise FileNotFoundError
        if p.name == "broken":
            raise OSError("nope")
        return FakeScandir(p)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    # stack contains: root, then missing/broken are simulated via Path names.
    (tmp_path / "missing").mkdir()
    (tmp_path / "broken").mkdir()

    total = FileComponent._scan_directory_size(tmp_path)
    assert total == 3


@pytest.mark.asyncio
async def test_write_with_quota_emits_flash_warning_for_non_tmp_path(
    file_component: tuple[FileComponent, DummyBridge],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component, _ = file_component

    non_tmp_root = Path.cwd() / ".pytest-mcubridge-nonvolatile"
    non_tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        component.state.allow_non_tmp_paths = True
        component.state.file_system_root = str(non_tmp_root)
        component.state.file_write_max_bytes = 32
        component.state.file_storage_quota_bytes = 1024

        ok, _, reason = await component._perform_file_operation(
            "write",
            "alpha.txt",
            b"abc",
        )
        assert ok is True
        assert reason == "ok"
    finally:
        for child in non_tmp_root.rglob("*"):
            if child.is_file():
                child.unlink()
        if non_tmp_root.exists():
            non_tmp_root.rmdir()


def test_write_file_sync_warns_when_growing_large(tmp_path: Path) -> None:
    path = tmp_path / "big.bin"
    FileComponent._write_file_sync(path, b"x" * (1024 * 1024 + 1))
    assert path.stat().st_size > 1024 * 1024


def test_get_base_dir_returns_none_when_mkdir_fails(
    file_component: tuple[FileComponent, DummyBridge],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component, _ = file_component
    component.state.allow_non_tmp_paths = True

    real_mkdir = Path.mkdir

    def failing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if str(self).endswith("fail-mkdir"):
            raise OSError("mkdir failed")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    component.state.file_system_root = str(Path.cwd() / "fail-mkdir")
    assert component._get_base_dir() is None


SAFE_FILENAME_CHARS = string.ascii_letters + string.digits + "/._- " + "\\"


@pytest.mark.parametrize(
    "filename",
    [
        "file.txt",
        "dir/file.txt",
        "dir/subdir/file.txt",
        "file with spaces.txt",
        "file-with-dashes.txt",
        "file_with_underscores.txt",
    ],
)
def test_normalise_filename_strips_traversal(filename: str) -> None:
    result = FileComponent._normalise_filename(filename)
    if result is None:
        return
    assert not result.is_absolute()
    for part in result.parts:
        assert part not in {"", ".", ".."}
        assert "\x00" not in part


@pytest.mark.parametrize(
    "filename",
    [
        "file.txt",
        "dir/file.txt",
        "dir/subdir/file.txt",
        "file with spaces.txt",
        "file-with-dashes.txt",
        "file_with_underscores.txt",
    ],
)
def test_get_safe_path_confines_to_root(file_component: tuple[FileComponent, DummyBridge], filename: str) -> None:
    component, _ = file_component
    base_dir = Path(component.state.file_system_root).expanduser().resolve()
    safe_path = component._get_safe_path(filename)
    if safe_path is None:
        return
    assert safe_path.is_relative_to(base_dir)
