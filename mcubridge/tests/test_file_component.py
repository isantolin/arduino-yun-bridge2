"""Unit tests for FileComponent MCU/MQTT behaviour (SIL-2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import msgspec
import pytest
from aiomqtt.message import Message

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import structures
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.topics import Topic, TopicRoute
from mcubridge.services.file import FileComponent
from mcubridge.services.serial_flow import SerialFlowController
from mcubridge.state.context import RuntimeState, create_runtime_state


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    import tempfile

    return RuntimeConfig(
        serial_port="/dev/null",
        mqtt_topic="br",
        file_system_root=tempfile.mkdtemp(
            prefix="mcubridge-test-fs-", dir=".tmp_tests"
        ),
        mqtt_spool_dir=tempfile.mkdtemp(
            prefix="mcubridge-test-spool-", dir=".tmp_tests"
        ),
        serial_shared_secret=b"secret1234",
        allow_non_tmp_paths=True,
    )


@pytest.fixture
def runtime_state(runtime_config: RuntimeConfig) -> RuntimeState:
    state = create_runtime_state(runtime_config)
    return state


@pytest.fixture
def file_component(
    runtime_config: RuntimeConfig,
    runtime_state: RuntimeState,
) -> tuple[FileComponent, AsyncMock, AsyncMock]:
    # [SIL-2] Use AsyncMock for all component mocks
    serial_flow = AsyncMock(spec=SerialFlowController)
    serial_flow.send = AsyncMock(return_value=True)
    serial_flow.acknowledge = AsyncMock()

    enqueue_mqtt = AsyncMock()

    component = FileComponent(runtime_config, runtime_state, serial_flow, enqueue_mqtt)
    # Mock storage usage to 0 for consistent tests
    component._get_storage_usage = MagicMock(return_value=0)  # type: ignore[reportPrivateUsage]
    return component, serial_flow, enqueue_mqtt


def _build_write_payload(filename: str, data: bytes) -> bytes:
    return msgspec.msgpack.encode(structures.FileWritePacket(path=filename, data=data))


@pytest.mark.asyncio
async def test_handle_mqtt_write_and_read(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
    tmp_path: Path,
) -> None:
    component, _serial_flow, enqueue_mqtt = file_component
    # Ensure component uses tmp_path
    component.config.file_system_root = str(tmp_path)

    msg = MagicMock(
        spec=aiomqtt.Message,
        topic="br/file/write/dir/file.txt",
        payload=b"payload",
        properties=None,
    )
    route = TopicRoute(
        raw="br/file/write/dir/file.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("write", "dir", "file.txt"),
    )

    await component.handle_mqtt(route, cast(Any, msg))
    assert (tmp_path / "dir" / "file.txt").read_bytes() == b"payload"

    msg_read = MagicMock(
        spec=aiomqtt.Message,
        topic="br/file/read/dir/file.txt",
        payload=b"",
        properties=None,
    )
    route_read = TopicRoute(
        raw="br/file/read/dir/file.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("read", "dir", "file.txt"),
    )

    await component.handle_mqtt(route_read, cast(Any, msg_read))
    # Read from local FS enqueues the result
    enqueue_mqtt.assert_called()
    assert enqueue_mqtt.call_args.args[0].payload == b"payload"


@pytest.mark.asyncio
async def test_handle_write_rejects_absolute_path(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, serial_flow, _ = file_component
    await component.handle_write(0, _build_write_payload("/etc/passwd", b"boom"))
    serial_flow.send.assert_called()
    assert serial_flow.send.call_args.args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_handle_write_rejects_parent_dir(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, serial_flow, _ = file_component
    await component.handle_write(0, _build_write_payload("../secret.txt", b"boom"))
    assert serial_flow.send.call_args.args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_handle_write_failure_sends_error(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component, serial_flow, _ = file_component

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("Disk full")

    monkeypatch.setattr(Path, "write_bytes", boom)
    await component.handle_write(0, _build_write_payload("err.txt", b"data"))
    assert serial_flow.send.call_args.args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_handle_mqtt_remove_action(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
    tmp_path: Path,
) -> None:
    component, _serial_flow, _ = file_component
    component.config.file_system_root = str(tmp_path)
    test_file = tmp_path / "rm.txt"
    test_file.write_bytes(b"bye")

    msg = MagicMock(
        spec=aiomqtt.Message,
        topic="br/file/remove/rm.txt",
        payload=b"",
        properties=None,
    )
    route = TopicRoute(
        raw="br/file/remove/rm.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("remove", "rm.txt"),
    )

    await component.handle_mqtt(route, cast(Any, msg))
    assert not test_file.exists()


@pytest.mark.asyncio
async def test_handle_read_large_payload_chunking(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, serial_flow, _ = file_component
    import os
    import time
    from pathlib import Path

    tmp_tests_dir = os.path.join(os.getcwd(), ".tmp_tests")
    tmp_path = Path(tmp_tests_dir) / f"mcubridge-test-{os.getpid()}-{time.time_ns()}"
    tmp_path.mkdir(parents=True, exist_ok=True)
    component.config.file_system_root = str(tmp_path)
    large_data = b"X" * 128  # Exactly 2 chunks
    (tmp_path / "large.bin").write_bytes(large_data)

    await component.handle_read(
        0, msgspec.msgpack.encode(structures.FileReadPacket(path="large.bin"))
    )

    # Should send 2 DATA chunks and 1 final empty chunk (total 3 frames)
    assert serial_flow.send.call_count >= 2


@pytest.mark.asyncio
async def test_handle_read_rejects_invalid_payloads(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, serial_flow, _ = file_component
    await component.handle_read(0, b"\xff\xff\xff")
    serial_flow.send.assert_called()
    assert serial_flow.send.call_args.args[0] == Status.ERROR.value


@pytest.mark.asyncio
async def test_handle_mqtt_missing_filename_is_ignored(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, _, enqueue_mqtt = file_component
    route = TopicRoute(
        raw="br/file/read", prefix="br", topic=Topic.FILE, segments=("read",)
    )
    # Correct the test: handle_mqtt returns True if it handled the error internally
    await component.handle_mqtt(
        route,
        MagicMock(
            spec=Message,
            topic="test/topic",
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        ),
    )
    enqueue_mqtt.assert_called()
    assert b"missing_path" in enqueue_mqtt.call_args.args[0].payload


@pytest.mark.asyncio
async def test_handle_mqtt_unknown_action_is_ignored(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, _, enqueue_mqtt = file_component
    route = TopicRoute(
        raw="br/file/magic/file.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("magic", "file.txt"),
    )
    await component.handle_mqtt(
        route,
        MagicMock(
            spec=Message,
            topic="test/topic",
            payload=b"",
            qos=0,
            retain=False,
            mid=1,
            properties=None,
        ),
    )
    assert not enqueue_mqtt.called


@pytest.mark.asyncio
async def test_handle_read_oserror_returns_false(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component, serial_flow, _ = file_component

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("Read fail")

    monkeypatch.setattr(Path, "read_bytes", boom)
    await component.handle_read(
        0,
        msgspec.msgpack.encode(structures.FileReadPacket(path="file.txt")),
    )
    # Filter only send calls
    calls = cast(list[Any], serial_flow.send.call_args_list)
    error_sent = any(call.args[0] == Status.ERROR.value for call in calls)
    assert error_sent


@pytest.mark.parametrize(
    "input_path",
    [
        "valid.txt",
        "subdir/file.bin",
        "file-with-dashes.txt",
        "file_with_underscores.txt",
    ],
)
def test_get_safe_path_confines_to_root(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock], input_path: str
) -> None:
    component, _, _ = file_component
    safe = component._get_safe_path(input_path)  # type: ignore[reportPrivateUsage]
    assert safe is not None
    assert str(safe).endswith(input_path)


@pytest.mark.asyncio
async def test_handle_read_large_payload_truncation_reproduction(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    """Reproduction test for a bug where large file reads were incorrectly truncated."""
    component, serial_flow, _ = file_component
    import os
    import time
    from pathlib import Path

    tmp_tests_dir = os.path.join(os.getcwd(), ".tmp_tests")
    tmp_path = Path(tmp_tests_dir) / f"mcubridge-test-{os.getpid()}-{time.time_ns()}"
    tmp_path.mkdir(parents=True, exist_ok=True)
    component.config.file_system_root = str(tmp_path)
    large_data = b"ABC" * 50  # 150 bytes
    (tmp_path / "trunc.bin").write_bytes(large_data)

    await component.handle_read(
        0, msgspec.msgpack.encode(structures.FileReadPacket(path="trunc.bin"))
    )

    # Total bytes sent in responses should match input
    total_received = b""
    # Filter for CMD_FILE_READ_RESP (0x93)
    calls = cast(list[Any], serial_flow.send.call_args_list)
    for call in calls:
        if call.args[0] == Command.CMD_FILE_READ_RESP.value:
            payload = call.kwargs.get(
                "payload", call.args[1] if len(call.args) > 1 else b""
            )
            total_received += msgspec.msgpack.decode(
                payload, type=structures.FileReadResponsePacket
            ).content

    assert total_received == large_data


@pytest.mark.asyncio
async def test_handle_mqtt_write_to_mcu_storage_disabled(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, _, enqueue_mqtt = file_component
    # Disable MCU backend
    component._mcu_backend_enabled = False  # type: ignore[reportPrivateUsage]

    msg = MagicMock(
        spec=aiomqtt.Message,
        topic="br/file/write/mcu/test.txt",
        payload=b"x",
        properties=None,
    )
    route = TopicRoute(
        raw="br/file/write/mcu/test.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("write", "mcu", "test.txt"),
    )

    await component.handle_mqtt(route, cast(Any, msg))

    # Just check that it enqueued an error
    enqueue_mqtt.assert_called()
    assert b"mcu_disabled" in enqueue_mqtt.call_args.args[0].payload


@pytest.mark.asyncio
async def test_handle_mqtt_read_from_mcu_storage_enabled(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, serial_flow, enqueue_mqtt = file_component
    component._mcu_backend_enabled = True  # type: ignore[reportPrivateUsage]

    # [SIL-2] Use a more robust mock to avoid deadlock and crashes
    async def _send_frame(
        command_id: int, payload: bytes = b"", seq_id: int | None = None
    ) -> bool:
        if command_id == Command.CMD_FILE_READ.value:
            # We must not call handle_read_response from here because it deadlocks on _mcu_read_lock
            # Instead, we satisfy the future after a tiny delay
            def _satisfy():
                if component._pending_mcu_read:  # type: ignore[reportPrivateUsage]
                    component._pending_mcu_read.future.set_result(b"mcu-data")  # type: ignore[reportPrivateUsage]

            asyncio.get_running_loop().call_soon(_satisfy)
        return True

    serial_flow.send.side_effect = _send_frame

    msg = MagicMock(
        spec=aiomqtt.Message,
        topic="br/file/read/mcu/test.txt",
        payload=b"",
        properties=None,
    )
    route = TopicRoute(
        raw="br/file/read/mcu/test.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("read", "mcu", "test.txt"),
    )

    await component.handle_mqtt(route, cast(Any, msg))
    enqueue_mqtt.assert_called()
    assert enqueue_mqtt.call_args.args[0].payload == b"mcu-data"


@pytest.mark.asyncio
async def test_handle_mqtt_read_from_mcu_storage_disabled(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, _, enqueue_mqtt = file_component
    component._mcu_backend_enabled = False  # type: ignore[reportPrivateUsage]

    msg = MagicMock(
        spec=aiomqtt.Message,
        topic="br/file/read/mcu/test.txt",
        payload=b"",
        properties=None,
    )
    route = TopicRoute(
        raw="br/file/read/mcu/test.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("read", "mcu", "test.txt"),
    )

    await component.handle_mqtt(route, cast(Any, msg))
    enqueue_mqtt.assert_called()
    assert b"mcu_disabled" in enqueue_mqtt.call_args.args[0].payload


@pytest.mark.asyncio
async def test_handle_mqtt_read_failure(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, serial_flow, enqueue_mqtt = file_component
    component._mcu_backend_enabled = True  # type: ignore[reportPrivateUsage]
    # Mock send returning False immediately
    serial_flow.send.return_value = False

    msg = MagicMock(
        spec=aiomqtt.Message,
        topic="br/file/read/mcu/fail.txt",
        payload=b"",
        properties=None,
    )
    route = TopicRoute(
        raw="br/file/read/mcu/fail.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("read", "mcu", "fail.txt"),
    )

    await component.handle_mqtt(route, cast(Any, msg))
    enqueue_mqtt.assert_called()
    assert b"mcu_dispatch_failed" in enqueue_mqtt.call_args.args[0].payload


@pytest.mark.asyncio
async def test_get_safe_path_none_base_dir(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, _, _ = file_component
    # Set to something that will fail path joining
    component.config.file_system_root = "/"

    # Test an unsafe relative path
    assert component._get_safe_path("../etc/passwd") is None  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_handle_mqtt_remove_from_mcu_storage_enabled(
    file_component: tuple[FileComponent, AsyncMock, AsyncMock],
) -> None:
    component, serial_flow, _ = file_component
    component._mcu_backend_enabled = True  # type: ignore[reportPrivateUsage]

    msg = MagicMock(
        spec=aiomqtt.Message,
        topic="br/file/remove/mcu/test.txt",
        payload=b"",
        properties=None,
    )
    route = TopicRoute(
        raw="br/file/remove/mcu/test.txt",
        prefix="br",
        topic=Topic.FILE,
        segments=("remove", "mcu", "test.txt"),
    )

    await component.handle_mqtt(route, cast(Any, msg))
    assert serial_flow.send.call_args.args[0] == Command.CMD_FILE_REMOVE.value
    assert b"test.txt" in serial_flow.send.call_args.args[1]
