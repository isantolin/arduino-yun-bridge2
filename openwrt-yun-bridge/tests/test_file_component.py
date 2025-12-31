from pathlib import Path
from unittest.mock import MagicMock

import pytest

from yunbridge.config.settings import RuntimeConfig
from yunbridge.const import (
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_METRICS_HOST,
    DEFAULT_METRICS_PORT,
    DEFAULT_MQTT_CAFILE,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_INTERVAL,
    MAX_PAYLOAD_SIZE,
    SUPERVISOR_DEFAULT_MAX_BACKOFF,
    SUPERVISOR_DEFAULT_MIN_BACKOFF,
    SUPERVISOR_DEFAULT_RESTART_INTERVAL,
)
from yunbridge.protocol import Command, Status
from yunbridge.rpc import protocol
from yunbridge.services.components.file import FileComponent
from yunbridge.state.context import RuntimeState


class DummyBridge:
    def __init__(self):
        self.sent_frames = []
        self.published = []

    def send_frame(self, frame):
        self.sent_frames.append((frame.command_id, frame.payload))

    async def publish(self, topic, payload, qos=0, retain=False):
        # Create a simple object to mimic QueuedPublish
        obj = MagicMock()
        obj.payload = payload
        self.published.append(obj)


@pytest.fixture()
def file_component(
    tmp_path: Path,
) -> tuple[FileComponent, DummyBridge]:
    bridge = DummyBridge()
    config = RuntimeConfig(
        serial_port="/dev/null",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=protocol.DEFAULT_SAFE_BAUDRATE,
        serial_shared_secret=b"testsecret",
        serial_retry_timeout=DEFAULT_SERIAL_RETRY_TIMEOUT,
        serial_response_timeout=DEFAULT_SERIAL_RESPONSE_TIMEOUT,
        serial_retry_attempts=1,
        serial_handshake_min_interval=DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
        serial_handshake_fatal_failures=DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
        mqtt_host="localhost",
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_tls=False,
        mqtt_cafile=DEFAULT_MQTT_CAFILE,
        mqtt_certfile=None,
        mqtt_keyfile=None,
        mqtt_user=None,
        mqtt_pass=None,
        mqtt_topic=DEFAULT_MQTT_TOPIC,
        mqtt_spool_dir=DEFAULT_MQTT_SPOOL_DIR,
        mqtt_queue_limit=DEFAULT_MQTT_QUEUE_LIMIT,
        file_system_root=str(tmp_path),
        file_write_max_bytes=DEFAULT_FILE_WRITE_MAX_BYTES,
        file_storage_quota_bytes=DEFAULT_FILE_STORAGE_QUOTA_BYTES,
        process_timeout=DEFAULT_PROCESS_TIMEOUT,
        process_max_output_bytes=DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
        process_max_concurrent=DEFAULT_PROCESS_MAX_CONCURRENT,
        console_queue_limit_bytes=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        mailbox_queue_limit=DEFAULT_MAILBOX_QUEUE_LIMIT,
        mailbox_queue_bytes_limit=DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        pending_pin_request_limit=DEFAULT_PENDING_PIN_REQUESTS,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        status_interval=DEFAULT_STATUS_INTERVAL,
        bridge_summary_interval=DEFAULT_BRIDGE_SUMMARY_INTERVAL,
        bridge_handshake_interval=DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
        debug_logging=False,
        allowed_commands=(),
        metrics_enabled=False,
        metrics_host=DEFAULT_METRICS_HOST,
        metrics_port=DEFAULT_METRICS_PORT,
        watchdog_enabled=False,
        watchdog_interval=DEFAULT_WATCHDOG_INTERVAL,
        supervisor_restart_interval=SUPERVISOR_DEFAULT_RESTART_INTERVAL,
        supervisor_min_backoff=SUPERVISOR_DEFAULT_MIN_BACKOFF,
        supervisor_max_backoff=SUPERVISOR_DEFAULT_MAX_BACKOFF,
    )
    state = RuntimeState(config)
    component = FileComponent(config, state)
    component.bind_serial_writer(bridge)
    component.bind_mqtt_client(bridge)
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


def _build_write_payload(filename: str, content: bytes) -> bytes:
    fname_bytes = filename.encode()
    return (
        bytes([len(fname_bytes)])
        + fname_bytes
        + len(content).to_bytes(2, "big")
        + content
    )


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
def test_get_safe_path_confines_to_root(
    file_component: tuple[FileComponent, DummyBridge],
    filename: str,
) -> None:
    component, _ = file_component
    path = component._get_safe_path(filename)
    root = Path(component.state.file_system_root).resolve()
    assert path.resolve().is_relative_to(root)
