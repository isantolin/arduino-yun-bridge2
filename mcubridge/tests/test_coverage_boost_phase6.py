"""Targeted branch coverage tests to push pure branch coverage above 95%."""

from __future__ import annotations
from mcubridge_client.spi import SpiDevice
from mcubridge_client.definitions import build_bridge_args
from mcubridge.transport.serial import SerialTransport
from mcubridge.state.context import ProcessContext, RuntimeState, create_runtime_state
from mcubridge.services.runtime import BridgeService
from mcubridge.protocol.structures import (
    PROTOBUF_CONTENT_TYPE,
    PendingCommand,
    TopicRoute,
)
from mcubridge.protocol.protocol import (
    Command,
    DatastoreAction,
    ShellAction,
    SpiAction,
    Status,
    SystemAction,
    Topic,
)
from mcubridge.protocol.frame import build_frame
from mcubridge.protocol import mcubridge_pb2 as pb

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from pytest_mock import MockerFixture
import pytest
from hypothesis import given, strategies as st

from mcubridge.config.logging import configure_logging
import mcubridge.config.settings as settings_mod
from mcubridge.config.settings import (
    RuntimeConfig,
    load_runtime_config_from_json,
)

_load_raw_config: Callable[[], tuple[dict[str, Any], str]] = getattr(settings_mod, "_load_raw_config")
_normalize_config_dict: Callable[[dict[str, Any]], tuple[dict[str, Any], bytes | None]] = getattr(
    settings_mod, "_normalize_config_dict"
)
_runtime_config_factory: Callable[..., RuntimeConfig] = getattr(settings_mod, "_runtime_config_factory")


@pytest.fixture
def test_config() -> RuntimeConfig:
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testsharedsecret",
        cloud_enabled=True,
        cloud_host="localhost",
        cloud_port=8443,
        topic_prefix="bridge",
        status_interval=1,
        bridge_summary_interval=0.0,
        bridge_handshake_interval=0.0,
        watchdog_enabled=False,
    )


@pytest.fixture
def mock_state(test_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(test_config)


# ==========================================
# 1. Config Settings & Logging Branches
# ==========================================


def test_settings_factory_bypass_defaults(mocker: MockerFixture) -> None:
    mocker.patch("mcubridge.config.settings.validate_config")
    cfg = _runtime_config_factory(
        bypass_defaults=True,
        serial_shared_secret="secretstring",
        serial_port="/dev/ttyS0",
        serial_baud=115200,
        serial_safe_baud=115200,
    )
    assert cfg.serial_port == "/dev/ttyS0"
    assert isinstance(cfg.serial_shared_secret, bytes)


def test_settings_load_raw_config_empty_uci(mocker: MockerFixture) -> None:
    mocker.patch("mcubridge.config.settings.get_uci_config", return_value={})
    cfg_dict, source = _load_raw_config()
    assert source == "defaults"
    assert "serial_port" in cfg_dict


def test_settings_load_runtime_config_from_json_unknown_override() -> None:
    data = {"serial_port": "/dev/ttyACM0"}
    cfg = load_runtime_config_from_json(
        data,
        overrides={"nonexistent_override_key": "ignored", "serial_baud": 230400},
    )
    assert cfg.serial_baud == 230400


@given(
    cloud_en=st.sampled_from(["1", "true", "yes", "on", True]),
    wd_en=st.sampled_from(["0", "false", "no", "off", False]),
    baud=st.sampled_from(["9600", "115200", 9600, 115200]),
    interval=st.floats(min_value=0.5, max_value=60.0),
    secret_str=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=32),
)
def test_settings_normalize_config_property(
    cloud_en: Any, wd_en: Any, baud: Any, interval: float, secret_str: str
) -> None:
    norm, secret = _normalize_config_dict(
        {
            "cloud_enabled": cloud_en,
            "cloud_tls": cloud_en,
            "watchdog_enabled": wd_en,
            "serial_baud": baud,
            "bridge_summary_interval": interval,
            "topic_prefix": "br",
            "serial_shared_secret": secret_str,
            "allowed_commands": "cat ls",
            "cloud_allow_datastore": cloud_en,
            "unknown_extra_key": "val",
        }
    )
    assert norm["cloud_enabled"] is True
    assert norm["cloud_tls"] is True
    assert norm["watchdog_enabled"] is False
    assert norm["serial_baud"] == baud
    assert norm["bridge_summary_interval"] == interval
    assert norm["topic_prefix"] == "br"
    assert secret == secret_str.encode()
    assert norm["allowed_commands"] == ["cat", "ls"]
    assert norm["topic_authorization"]["datastore_get"] is True
    assert norm["topic_authorization"]["datastore_put"] is True
    assert norm["unknown_extra_key"] == "val"

    _, secret_none = _normalize_config_dict({"serial_shared_secret": None})
    assert secret_none is None


def test_logging_discover_syslog_var_run_branch(test_config: RuntimeConfig, mocker: MockerFixture) -> None:
    def _mock_exists(path_obj: Path) -> bool:
        return str(path_obj) == "/var/run/log"

    mocker.patch.dict("os.environ", {}, clear=True)
    mocker.patch.object(Path, "exists", _mock_exists)
    mock_syslog = mocker.patch("mcubridge.config.logging.SysLogHandler")
    configure_logging(test_config)
    assert mock_syslog.called


# ==========================================
# 2. Context & Lifecycle Branches
# ==========================================


def test_context_mark_states_without_link_sync_event(test_config: RuntimeConfig) -> None:
    state = create_runtime_state(test_config)
    setattr(state, "link_sync_event", None)

    state.connection_fsm.disconnect()
    assert state.state == "disconnected"

    state.connection_fsm.synchronize()
    assert state.state == "synchronized"


def test_context_configure_safe_close_sync_resource(test_config: RuntimeConfig) -> None:
    state = create_runtime_state(test_config)

    class SyncCloseResource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> str:
            self.closed = True
            return "closed-synchronously"

    res = SyncCloseResource()
    state.datastore_cache = cast(Any, res)
    state.configure()
    assert res.closed is True


def test_context_cleanup_none_handle_process(test_config: RuntimeConfig) -> None:
    state = create_runtime_state(test_config)

    # ProcessContext with None handle
    ctx = ProcessContext(cast(Any, None))
    state.running_processes[12345] = ctx
    state.cleanup()
    assert len(state.running_processes) == 0


# ==========================================
# 3. Serial Transport Branches
# ==========================================


@pytest.mark.asyncio
async def test_serial_transport_methods_with_none_serial(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    transport.serial = None

    # 1. _switch_local_baudrate when serial is None
    switch_baud: Callable[[int], None] = getattr(transport, "_switch_local_baudrate")
    switch_baud(115200)

    # 2. reset() when _current is None
    setattr(transport, "_current", None)
    await transport.reset()

    # 3. _toggle_dtr when serial is None
    toggle_dtr: Callable[[], Awaitable[None]] = getattr(transport, "_toggle_dtr")
    await toggle_dtr()

    # 4. stop() when serial is None
    await transport.stop()
    stop_event: asyncio.Event = getattr(transport, "_stop_event")
    assert stop_event.is_set()

    # 5. _check_baudrate_fallback when baud == safe_baud
    test_config.serial_baud = test_config.serial_safe_baud
    setattr(transport, "_consecutive_crc_errors", test_config.serial_fallback_threshold - 1)
    fallback_fn: Callable[[], Awaitable[None]] = getattr(transport, "_check_baudrate_fallback")
    await fallback_fn()


@pytest.mark.asyncio
async def test_serial_transport_correlate_frame_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    correlate_fn: Callable[[int, bytes], None] = getattr(transport, "_correlate_frame")

    # 1. ACK with non-matching ack_target
    curr1 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    setattr(transport, "_current", curr1)
    ack_pkt = pb.AckPacket(command_id=Command.CMD_ANALOG_WRITE.value)
    correlate_fn(Status.ACK.value, ack_pkt.SerializeToString())
    assert curr1.ack_received is False

    # 2. ACK with matching ack_target but non-empty expected_resp_ids
    curr2 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    setattr(transport, "_current", curr2)
    ack_matching = pb.AckPacket(command_id=Command.CMD_DIGITAL_WRITE.value)
    correlate_fn(Status.ACK.value, ack_matching.SerializeToString())
    assert curr2.ack_received is True
    assert curr2.success is None

    # 3. Status in SERIAL_SUCCESS_STATUS_CODES with non-empty expected_resp_ids
    curr3 = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    setattr(transport, "_current", curr3)
    correlate_fn(Status.OK.value, b"")
    assert curr3.success is None


@pytest.mark.asyncio
async def test_serial_process_packet_negotiating_non_baud_cmd(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    setattr(transport, "_negotiating", True)
    fut = asyncio.get_running_loop().create_future()
    setattr(transport, "_negotiation_future", fut)

    # Frame with command that is NOT CMD_SET_BAUDRATE_RESP
    frame_bytes = build_frame(
        command_id=Command.CMD_GET_VERSION.value,
        payload=b"",
        sequence_id=1,
    )
    from mcubridge.transport.serial import cobsr

    encoded = cobsr.encode(frame_bytes)
    proc_packet_fn: Callable[[bytes], Awaitable[None]] = getattr(transport, "_process_packet")
    await proc_packet_fn(encoded)
    assert not fut.done()


# ==========================================
# 4. Runtime Service Dispatch & Edge Branches
# ==========================================


@pytest.mark.asyncio
async def test_runtime_file_dispatch_local_methods_none_path(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    req = pb.CloudQueuedPublish(topic_name="bridge/file/read/test", payload=b"")

    # Call with unsafe path so that _get_safe_path returns None
    route = TopicRoute(
        raw="bridge/file/read/../../secret",
        prefix=test_config.topic_prefix,
        topic=Topic.FILE,
        segments=("read", "..", "..", "secret"),
    )
    handle_file: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file")
    await handle_file(route, req)
    assert serial.send.call_count == 0


@pytest.mark.asyncio
async def test_runtime_handle_mcu_frame_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. serial is None
    svc.serial = None
    await svc.handle_mcu_frame(Command.CMD_GET_VERSION.value, 1, b"")
    assert not serial.send.called

    # 2. unhandled command with known response_to_request mapping
    svc.serial = serial
    mock_state.connection_fsm.synchronize()
    await svc.handle_mcu_frame(Command.CMD_GET_VERSION_RESP.value, 1, pb.VersionResponse().SerializeToString())
    assert not serial.send.called

    # 3. Unknown command without response mapping
    await svc.handle_mcu_frame(9999, 1, b"")
    assert serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_request_route_none_and_inbound_props(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. Route is None (topic not matching prefix)
    await svc.handle_request(pb.CloudQueuedPublish(topic_name="unmatched/topic", payload=b""))
    assert not serial.send.called

    # 2. Inbound object with properties containing ResponseTopic and CorrelationData
    class InboundProps:
        ResponseTopic = "cloud/resp"
        CorrelationData = b"corr456"

    class InboundObj:
        properties = InboundProps()
        topic = "bridge/unmatched"
        payload = b"testpayload"

    await svc.handle_request(InboundObj())
    assert not serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_console_empty_payload(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_console: Callable[..., Awaitable[None]] = getattr(svc, "_handle_console")
    await handle_console(None, pb.CloudQueuedPublish(topic_name="bridge/console", payload=b""))
    assert not serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_datastore_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_ds: Callable[..., Awaitable[None]] = getattr(svc, "_handle_datastore")

    # 1. PUT with oversized payload (> 512 bytes)
    route_put = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.PUT.value, "mykey")
    )
    await handle_ds(route_put, pb.CloudQueuedPublish(payload=b"x" * 600))
    assert not serial.send.called

    # 2. GET cache miss without "request" suffix in remainder
    route_get = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.GET.value, "missingkey")
    )
    await handle_ds(route_get, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called

    # 3. Unknown identifier
    route_unknown = TopicRoute(raw="", prefix="bridge", topic=Topic.DATASTORE, segments=("unknown_act", "key"))
    await handle_ds(route_unknown, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_mailbox_unknown_identifier(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    route = TopicRoute(raw="", prefix="bridge", topic=Topic.MAILBOX, segments=("unknown",))
    handle_mb: Callable[..., Awaitable[None]] = getattr(svc, "_handle_mailbox")
    await handle_mb(route, pb.CloudQueuedPublish(payload=b"test"))
    assert not serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_file_unhandled_action_and_failed_writes(
    test_config: RuntimeConfig, mock_state: RuntimeState, tmp_path: Path, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_file: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file")
    h_mcu_w: Callable[..., Awaitable[bool]] = getattr(svc, "_handle_file_mcu_write")
    h_mcu_rm: Callable[..., Awaitable[bool]] = getattr(svc, "_handle_file_mcu_remove")
    h_loc_w: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file_local_write")
    h_loc_r: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file_local_read")

    # 1. Unregistered (is_mcu, act) tuple
    route_unhandled = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=("custom_act", "mcu/file"))
    await handle_file(route_unhandled, pb.CloudQueuedPublish(payload=b"data"))

    # 2. MCU write send fails
    serial.send = AsyncMock(return_value=False)
    await h_mcu_w("mcu/test.txt", pb.CloudQueuedPublish(payload=b"data"))
    assert serial.send.called

    # 3. MCU remove with serial=None
    svc.serial = None
    await h_mcu_rm("mcu/test.txt", pb.CloudQueuedPublish(payload=b""))
    svc.serial = serial

    # 4. Local write fails quota
    mocker.patch.object(svc, "_write_with_quota", return_value=False)
    await h_loc_w("f.txt", pb.CloudQueuedPublish(payload=b"data"))

    # 5. Local read when path is not a file
    await h_loc_r("not_a_file", pb.CloudQueuedPublish(topic_name="bridge/file/read"))

    # 6. Local read with response topic (skips re-publishing)
    real_file = tmp_path / "real.txt"
    real_file.write_text("hello")
    await h_loc_r("real.txt", pb.CloudQueuedPublish(topic_name="bridge/file/read/response"))
    assert real_file.exists()


@pytest.mark.asyncio
async def test_runtime_handle_shell_branches(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_shell: Callable[..., Awaitable[None]] = getattr(svc, "_handle_shell")

    # 1. Unregistered shell action
    route_unregistered = TopicRoute(raw="", prefix="bridge", topic=Topic.SHELL, segments=("unknown_act",))
    await handle_shell(route_unregistered, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called

    # 2. run_async with protobuf payload bytes starting with \x0a
    proto_cmd = pb.ProcessRunAsync(command="echo hello").SerializeToString()
    route_run = TopicRoute(raw="", prefix="bridge", topic=Topic.SHELL, segments=(ShellAction.RUN_ASYNC.value,))
    mock_run = mocker.patch.object(svc, "run_process", new_callable=AsyncMock, return_value=123)
    await handle_shell(route_run, pb.CloudQueuedPublish(payload=proto_cmd))
    assert mock_run.called

    # 3. kill with unknown pid
    await svc.kill_process(99999)
    assert 99999 not in svc.state.running_processes


@pytest.mark.asyncio
async def test_runtime_handle_spi_and_pin_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_spi: Callable[..., Awaitable[None]] = getattr(svc, "_handle_spi")
    handle_pin: Callable[..., Awaitable[None]] = getattr(svc, "_handle_pin")

    # 1. SPI transfer with empty payload
    route_spi_xfer = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=(SpiAction.TRANSFER.value,))
    await handle_spi(route_spi_xfer, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called

    # 2. Pin handler with unknown action in segments[1]
    route_pin = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13", "unknown_action"))
    await handle_pin(route_pin, pb.CloudQueuedPublish(payload=b"1"))
    assert not serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_system_free_memory_non_bytes(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    serial.send = AsyncMock(return_value=False)
    svc = BridgeService(test_config, mock_state, serial)
    handle_sys: Callable[..., Awaitable[None]] = getattr(svc, "_handle_system")

    route = TopicRoute(raw="", prefix="bridge", topic=Topic.SYSTEM, segments=(SystemAction.FREE_MEMORY.value, "get"))
    await handle_sys(route, pb.CloudQueuedPublish(payload=b""))
    assert serial.send.called


@pytest.mark.asyncio
async def test_runtime_request_mcu_version_empty_topic(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    v_resp = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    serial.send = AsyncMock(return_value=v_resp)
    svc = BridgeService(test_config, mock_state, serial)

    mocker.patch("mcubridge.services.runtime.get_topic_for_message", return_value="")
    req_ver: Callable[[], Awaitable[bool]] = getattr(svc, "_request_mcu_version")
    ok = await req_ver()
    assert ok is True


@pytest.mark.asyncio
async def test_runtime_monitor_process_none_ctx(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    # Monitor non-existent process
    mon_proc: Callable[[int], Awaitable[None]] = getattr(svc, "_monitor_process")
    await mon_proc(99999)
    assert 99999 not in svc.state.running_processes


@pytest.mark.asyncio
async def test_runtime_cloud_session_non_command_envelope(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # Mock stream yielding event envelope (neither pong nor command_request)
    event_env = pb.CloudEnvelope(
        protocol_version=2,
        event=pb.EventNotification(event_type="custom", severity="info", description="test"),
    )

    class MockStream:
        def __init__(self, items: list[pb.CloudEnvelope]) -> None:
            self._items = items

        async def __aenter__(self) -> MockStream:
            return self

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass

        def __aiter__(self) -> MockStream:
            self._iter = iter(self._items)
            return self

        async def __anext__(self) -> pb.CloudEnvelope:
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

        async def send_message(self, _msg: Any) -> None:
            pass

    mock_stub = MagicMock()
    mock_stub.Session.open.return_value = MockStream([event_env])

    mocker.patch("mcubridge.services.runtime.Channel")
    mocker.patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub)
    mocker.patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock)
    mocker.patch.object(svc, "_send_cloud_event", new_callable=AsyncMock)
    await svc.connect_cloud_session(None)
    assert getattr(svc, "_cloud_stream") is None
    assert svc.state.cloud_fsm.spooling_degraded.is_active


# ==========================================
# 5. Definitions & Client SPI Branches
# ==========================================


def test_definitions_build_bridge_args_empty(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {}, clear=True)
    args = build_bridge_args(host="127.0.0.1", port=8443, device_id="test-dev", topic_prefix="")
    assert args == {"host": "127.0.0.1", "port": 8443, "device_id": "test-dev"}
    with pytest.raises(ValueError, match="Explicit target device_id is required"):
        build_bridge_args(host="127.0.0.1", port=8443)


@pytest.mark.asyncio
async def test_client_spi_transfer_branches() -> None:
    def _mock_spi_transfer(req: pb.SpiTransfer) -> pb.SpiTransferResponse:
        return pb.SpiTransferResponse(data=req.data)

    stub = AsyncMock()
    stub.SpiTransfer = AsyncMock(side_effect=_mock_spi_transfer)
    dev = SpiDevice(stub)

    # Transfer when not active (auto calls begin) and with list data
    res = await dev.transfer([1, 2, 3])
    assert res == b"\x01\x02\x03"


# ==========================================
# 6. Deep Runtime & Protocol Branch Tests
# ==========================================


@pytest.mark.asyncio
async def test_runtime_cloud_spool_locked_limit_errors(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    mock_state.cloud_queue_limit = 1
    spool_locked: Callable[..., Awaitable[bool]] = getattr(svc, "_spool_cloud_message_locked")

    # Case 1: spool.popleft raises IndexError during limit trim
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [2, 0]
    mock_spool.popleft = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.append = AsyncMock(return_value=None)
    setattr(svc, "_cloud_spool", mock_spool)
    msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"data")
    assert await spool_locked(msg) is True

    # Case 2: spool.popleft raises OSError during limit trim
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [2, 0]
    mock_spool.popleft = AsyncMock(side_effect=OSError("IO failure"))
    mock_spool.append = AsyncMock(return_value=None)
    setattr(svc, "_cloud_spool", mock_spool)
    assert await spool_locked(msg) is True

    # Case 3: spool.append raises OSError
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [0, 0]
    mock_spool.append = AsyncMock(side_effect=OSError("Disk full"))
    setattr(svc, "_cloud_spool", mock_spool)
    assert await spool_locked(msg) is False


@pytest.mark.asyncio
async def test_runtime_flush_cloud_spool_corrupt_and_errors(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    setattr(svc, "_cloud_stream", AsyncMock())
    flush_locked: Callable[[], Awaitable[None]] = getattr(svc, "_flush_cloud_spool_locked")

    # Case 1: Corrupt item with spool.popleft raising IndexError
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.vacuum = AsyncMock()
    setattr(svc, "_cloud_spool", mock_spool)
    await flush_locked()

    # Case 2: Corrupt item with spool.popleft raising OSError
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(side_effect=OSError("IO Error"))
    mock_spool.vacuum = AsyncMock()
    setattr(svc, "_cloud_spool", mock_spool)
    await flush_locked()

    # Case 3: Corrupt item with valid popleft
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(return_value=None)
    mock_spool.vacuum = AsyncMock()
    setattr(svc, "_cloud_spool", mock_spool)
    await flush_locked()

    # Case 4: Valid item with popleft raising OSError after publish
    valid_bytes = pb.CloudQueuedPublish(topic_name="br/t", payload=b"p").SerializeToString()
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=valid_bytes)
    mock_spool.popleft = AsyncMock(side_effect=OSError("DB lock error"))
    mock_spool.vacuum = AsyncMock()
    setattr(svc, "_cloud_spool", mock_spool)
    mocker.patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True)
    await flush_locked()

    # Case 5: Valid item with normal completion
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=valid_bytes)
    mock_spool.popleft = AsyncMock(return_value=None)
    mock_spool.vacuum = AsyncMock()
    setattr(svc, "_cloud_spool", mock_spool)
    mocker.patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True)
    await flush_locked()

    # Case 6: Vacuum raising OSError
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [0, 0, 0]
    mock_spool.vacuum = AsyncMock(side_effect=OSError("Vacuum disk fail"))
    setattr(svc, "_cloud_spool", mock_spool)
    await flush_locked()
    assert mock_spool.vacuum.called


@pytest.mark.asyncio
async def test_runtime_handle_datastore_empty_key_and_request_miss(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_ds: Callable[..., Awaitable[None]] = getattr(svc, "_handle_datastore")

    # Empty key
    route_empty = TopicRoute(raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.PUT.value,))
    await handle_ds(route_empty, pb.CloudQueuedPublish(payload=b"val"))

    # GET with 'request' in remainder and cache miss
    route_req = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.GET.value, "key1", "request")
    )
    mock_pub = mocker.patch.object(svc, "publish_datastore_value", new_callable=AsyncMock)
    await handle_ds(route_req, pb.CloudQueuedPublish(payload=b""))
    assert mock_pub.call_count == 1
    assert mock_pub.call_args[0][0] == "key1/request"
    assert mock_pub.call_args[1]["error"] == "datastore-miss"


@pytest.mark.asyncio
async def test_runtime_handle_mailbox_edge_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_mb: Callable[..., Awaitable[None]] = getattr(svc, "_handle_mailbox")

    # Test handling when serial is None
    svc.serial = None
    route = TopicRoute(raw="", prefix="bridge", topic=Topic.MAILBOX, segments=("write",))
    await handle_mb(route, pb.CloudQueuedPublish(payload=b"hi"))
    assert not serial.send.called

    # Read with empty incoming queue
    svc.serial = serial
    await mock_state.mailbox_incoming_queue.clear()
    route_read = TopicRoute(raw="", prefix="bridge", topic=Topic.MAILBOX, segments=("read",))
    await handle_mb(route_read, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called


@pytest.mark.asyncio
async def test_runtime_handle_file_and_shell_edge_branches(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_file: Callable[..., Awaitable[None]] = getattr(svc, "_handle_file")
    handle_shell: Callable[..., Awaitable[None]] = getattr(svc, "_handle_shell")
    handle_run_async: Callable[..., Awaitable[None]] = getattr(svc, "_handle_shell_run_async")

    # File with serial=None
    svc.serial = None
    route_file = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=("read", "test.txt"))
    await handle_file(route_file, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called

    # File with empty remainder
    svc.serial = serial
    route_no_rem = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=())
    await handle_file(route_no_rem, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called

    # File safe path returning None for unsafe target
    route_unsafe = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=("read", "../../../etc/shadow"))
    mocker.patch.object(svc, "_get_safe_path", return_value=None)
    await handle_file(route_unsafe, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called

    # Shell with empty segments
    route_shell_empty = TopicRoute(raw="", prefix="bridge", topic=Topic.SHELL, segments=())
    await handle_shell(route_shell_empty, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called

    # Shell run_async with inbound properties ContentType
    class Props:
        ContentType = "application/x-protobuf"

    class InboundWithProps:
        payload = pb.ProcessRunAsync(command="echo prop").SerializeToString()
        properties = Props()

    mock_rp = mocker.patch.object(svc, "run_process", new_callable=AsyncMock, return_value=123)
    await handle_run_async(0, cast(Any, InboundWithProps()))
    assert mock_rp.called

    # Shell kill raising ProcessLookupError
    mock_ctx = ProcessContext(AsyncMock())
    mock_state.running_processes[777] = mock_ctx
    mocker.patch.object(svc, "_terminate_process", side_effect=ProcessLookupError("No such process"))
    await svc.kill_process(777)
    assert 777 not in mock_state.running_processes


@pytest.mark.asyncio
async def test_runtime_handle_spi_and_pin_edge_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_spi: Callable[..., Awaitable[None]] = getattr(svc, "_handle_spi")
    handle_pin: Callable[..., Awaitable[None]] = getattr(svc, "_handle_pin")

    # SPI with serial=None
    svc.serial = None
    route_spi = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("begin",))
    await handle_spi(route_spi, pb.CloudQueuedPublish(payload=b""))
    assert not serial.send.called

    # SPI config with invalid protobuf
    svc.serial = serial
    route_spi_cfg = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("config",))
    await handle_spi(route_spi_cfg, pb.CloudQueuedPublish(payload=b"not-proto"))
    assert not serial.send.called

    # SPI transfer with non-bytes response
    serial.send.return_value = False
    route_spi_xfer = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("transfer",))
    await handle_spi(route_spi_xfer, pb.CloudQueuedPublish(payload=b"data"))
    assert serial.send.called

    # Pin with serial=None
    serial.send.reset_mock()
    svc.serial = None
    route_pin = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13", "write"))
    await handle_pin(route_pin, pb.CloudQueuedPublish(payload=b"1"))
    assert not serial.send.called

    # Pin with invalid segments (< 2 segments or non-digit pin)
    svc.serial = serial
    route_pin_invalid = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("invalid_pin", "write"))
    await handle_pin(route_pin_invalid, pb.CloudQueuedPublish(payload=b"1"))
    assert not serial.send.called

    route_pin_short = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13",))
    await handle_pin(route_pin_short, pb.CloudQueuedPublish(payload=b"1"))
    assert serial.send.called


@pytest.mark.asyncio
async def test_runtime_request_mcu_version_failures(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    req_ver: Callable[[], Awaitable[bool]] = getattr(svc, "_request_mcu_version")

    # 1. serial is None
    svc.serial = None
    assert await req_ver() is False

    # 2. serial.send returns False
    svc.serial = serial
    serial.send.return_value = False
    assert await req_ver() is False


# ==========================================
# 7. Handshake FSM and Timeout Branches
# ==========================================


@pytest.mark.asyncio
async def test_handshake_wait_confirmation_already_synchronized(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    from mcubridge.services.handshake import SerialHandshakeManager

    hs = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    mock_state.connection_fsm.synchronize()

    wait_sync: Callable[[bytes], Awaitable[bool]] = getattr(hs, "_wait_for_link_sync_confirmation")
    confirmed = await wait_sync(b"nonce")
    assert confirmed is True


@pytest.mark.asyncio
async def test_handshake_sync_state_permutations(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    from mcubridge.services.handshake import HandshakeState, SerialHandshakeManager

    mock_send = AsyncMock(return_value=True)
    hs = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=mock_send,
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    sync_attempt: Callable[[], Awaitable[bool]] = getattr(hs, "_synchronize_attempt")

    # 1. State is FAULT after send_link_sync
    mocker.patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True)
    hs.fsm_state = HandshakeState.FAULT
    assert await sync_attempt() is False

    # 2. Confirmed is False, and state is FAULT
    hs.fsm_state = HandshakeState.SYNCING

    async def _mock_wait_fault(nonce: bytes) -> bool:
        hs.fsm_state = HandshakeState.FAULT
        return False

    setattr(hs, "_wait_for_link_sync_confirmation", _mock_wait_fault)
    assert await sync_attempt() is False

    # 3. Confirmed is False, pending_nonce != nonce
    mocker.patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=False)
    hs.fsm_state = HandshakeState.SYNCING
    mock_state.link_handshake_nonce = b"different_nonce"
    assert await sync_attempt() is False

    # 4. Confirmed is True, current_state is SYNCHRONIZED
    mocker.patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=True)
    hs.fsm_state = HandshakeState.SYNCHRONIZED
    assert await sync_attempt() is True


@pytest.mark.asyncio
async def test_handshake_resp_rate_limit_and_secret_none(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    import time
    from mcubridge.services.handshake import SerialHandshakeManager

    test_config.serial_handshake_min_interval = 5.0
    hs = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )

    # 1. Rate limit branch
    mock_state.link_handshake_nonce = b"expected_nonce_12b"
    mock_state.handshake_rate_until = time.monotonic() + 10.0
    pkt = pb.LinkSync(nonce=b"expected_nonce_12b", tag=b"tag")
    assert await hs.handle_link_sync_resp(1, pkt) is False

    # 2. Shared secret is empty / None on successful sync
    test_config.serial_handshake_min_interval = 0.0
    test_config.serial_shared_secret = b""
    mock_state.handshake_rate_until = 0.0
    nonce = b"expected_12b_nonce"
    mock_state.link_handshake_nonce = nonce
    tag = hs.calculate_handshake_tag(b"", nonce)
    mock_state.link_expected_tag = tag
    pkt_matching = pb.LinkSync(nonce=nonce, tag=tag)
    mocker.patch.object(hs, "_handle_handshake_success", new_callable=AsyncMock)
    mocker.patch.object(hs, "_fetch_capabilities_with_delay", new_callable=AsyncMock)
    assert await hs.handle_link_sync_resp(2, pkt_matching) is True
    assert mock_state.link_session_key is None


@pytest.mark.asyncio
async def test_handshake_capabilities_resp_future_none(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.services.handshake import SerialHandshakeManager

    hs = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    setattr(hs, "_capabilities_future", None)
    assert await hs.handle_capabilities_resp(1, b"") is True


# ==========================================
# 8. Serial, Structures, Runtime & Metrics
# ==========================================


@pytest.mark.asyncio
async def test_serial_transport_read_loop_empty_view_and_service_none(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    mock_serial = AsyncMock()

    # 1. read_loop delimiter-only packet (empty packet_view)
    mock_serial.readuntil.side_effect = [b"\x00", asyncio.IncompleteReadError(partial=b"", expected=None)]
    read_loop: Callable[..., Awaitable[None]] = getattr(transport, "_read_loop")
    await read_loop(mock_serial)

    # 2. _correlate_frame with empty ACK payload
    curr_cmd = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[],
    )
    setattr(transport, "_current", curr_cmd)
    correlate_fn: Callable[[int, bytes], None] = getattr(transport, "_correlate_frame")
    correlate_fn(Status.ACK.value, b"")
    assert curr_cmd.ack_received is True
    assert curr_cmd.success


@pytest.mark.asyncio
async def test_runtime_teardown_lmdb_errors(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    import lmdb

    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    mock_spool = AsyncMock()
    mock_spool.close.side_effect = lmdb.Error("Spool close fail")
    setattr(svc, "_cloud_spool", mock_spool)

    mock_cache = AsyncMock()
    mock_cache.close.side_effect = OSError("Cache close fail")
    mock_state.datastore_cache = mock_cache

    mock_mb = AsyncMock()
    mock_mb.close.side_effect = lmdb.Error("Mailbox close fail")
    mock_state.mailbox_queue = mock_mb

    mock_mbin = AsyncMock()
    mock_mbin.close.side_effect = OSError("Mailbox in close fail")
    mock_state.mailbox_incoming_queue = mock_mbin

    mocker.patch("mcubridge.services.runtime.STATUS_FILE")
    mocker.patch.object(svc, "cleanup")
    mocker.patch("asyncio.TaskGroup", side_effect=ExceptionGroup("tasks", [RuntimeError("Teardown trigger")]))
    with pytest.raises(ExceptionGroup):
        await svc.run()


@pytest.mark.asyncio
async def test_runtime_poll_process_eof_and_xoff(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. _poll_process with finished process having stdout and stderr at EOF
    mock_handle = MagicMock()
    mock_handle.returncode = 0
    mock_handle.stdout = None
    mock_handle.stderr = None

    ctx = ProcessContext(mock_handle)
    mock_state.running_processes[555] = ctx
    resp = await svc.poll_process(555)
    assert resp.finished is True
    assert 555 not in mock_state.running_processes

    # 2. _handle_mcu_xoff
    handle_xoff: Callable[..., Awaitable[None]] = getattr(svc, "_handle_mcu_xoff")
    await handle_xoff(1, None)
    assert mock_state.mcu_is_paused is True
    assert not mock_state.serial_tx_allowed.is_set()

    # 3. _on_mcu_console_write with empty data
    on_console: Callable[..., Awaitable[None]] = getattr(svc, "_on_mcu_console_write")
    await on_console(1, pb.ConsoleWrite(data=b""))

    # 4. _on_mcu_datastore_get with serial=None
    svc.serial = None
    on_ds_get: Callable[..., Awaitable[bool]] = getattr(svc, "_on_mcu_datastore_get")
    assert await on_ds_get(1, pb.DatastoreGet(key="k")) is False


# # ==========================================
# 9. Daemon, Logging, Settings, Security & Frame
# ==========================================


def test_daemon_shared_secret_none_and_app_main(mocker: MockerFixture) -> None:
    from mcubridge.daemon import app, run_daemon

    # 1. run_daemon without shared secret
    mock_cfg = RuntimeConfig()
    mock_cfg.serial_shared_secret = b""
    mocker.patch("mcubridge.daemon.load_runtime_config", return_value=mock_cfg)
    mocker.patch("mcubridge.services.runtime.BridgeService.run", new_callable=AsyncMock)
    mock_runner_cls = mocker.patch("mcubridge.daemon.asyncio.Runner")
    mock_runner = MagicMock()

    def _mock_run(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()

    mock_runner.run.side_effect = _mock_run
    mock_runner_cls.return_value.__enter__.return_value = mock_runner
    run_daemon()
    assert mock_runner.run.called

    # 2. app(args=None)
    mock_rd = mocker.patch("mcubridge.daemon.run_daemon")
    app(None)
    assert mock_rd.called


def test_logging_no_syslog_available(test_config: RuntimeConfig, mocker: MockerFixture) -> None:
    import logging
    from mcubridge.config.logging import configure_logging

    mocker.patch.dict("os.environ", {}, clear=True)
    mocker.patch("pathlib.Path.exists", return_value=False)
    configure_logging(test_config)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers)


def test_settings_raw_config_edge_branches(mocker: MockerFixture) -> None:
    from mcubridge.config.common import get_default_config
    from mcubridge.config.settings import load_runtime_config, load_runtime_config_from_json

    # 1. Non list/tuple for repeated field (line 173->176)
    cfg1 = load_runtime_config(overrides={"allowed_commands": 123})
    assert cfg1 is not None

    # 2. Topic auth coerced to None (line 190->192)
    cfg2 = load_runtime_config(overrides={"allow_datastore": object()})
    assert cfg2 is not None

    # 3. Defaults with bytes value
    defaults = get_default_config()
    defaults["serial_port"] = b"/dev/ttyATH0"
    mocker.patch("mcubridge.config.settings.get_default_config", return_value=defaults)
    cfg3 = load_runtime_config_from_json("{}")
    assert cfg3.serial_port == "/dev/ttyATH0"

    # 4. JSON dict with serial_shared_secret
    cfg4 = load_runtime_config_from_json({"serial_shared_secret": "json_secret"})
    assert cfg4.serial_shared_secret == b"json_secret"

    # 5. Allowed commands as None
    cfg5 = load_runtime_config(overrides={"allowed_commands": None})
    assert list(cfg5.allowed_commands) == []

    # 6. Overrides with serial_shared_secret None raises validation error
    with pytest.raises(ValueError, match="serial_shared_secret"):
        load_runtime_config(overrides={"serial_shared_secret": None})


def test_security_self_test_chacha_invalid_length(mocker: MockerFixture) -> None:
    from mcubridge.security.security import verify_crypto_integrity

    mocker.patch("mcubridge.security.security.ChaCha20Poly1305.encrypt", return_value=b"short")
    assert verify_crypto_integrity() is False


def test_watchdog_kick_state_none(mocker: MockerFixture) -> None:
    from mcubridge.watchdog import WatchdogKeepalive

    wd = WatchdogKeepalive(interval=10.0, state=None)
    setattr(wd, "_token", b"W")
    mock_write = mocker.patch.object(wd, "_write")
    wd.kick()
    assert mock_write.called


def test_protocol_frame_unrecognized_protobuf_descriptor() -> None:
    from mcubridge.protocol.frame import build_frame

    # ProtobufMessage not in _PAYLOAD_FIELD_MAP
    cfg = pb.RuntimeConfig()
    frame_bytes = build_frame(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        sequence_id=1,
        payload=cfg,
        session_key=None,
    )
    assert len(frame_bytes) > 0


def test_daemon_metrics_build_info_package_found(mocker: MockerFixture) -> None:
    from mcubridge.state.metrics import DaemonMetrics

    mocker.patch("importlib.metadata.version", return_value="2.0.0")
    dm = DaemonMetrics()
    assert dm.build_info is not None


# ==========================================
# 10. Handshake, Metrics and Runtime Edge Permutations
# ==========================================


@pytest.mark.asyncio
async def test_handshake_sync_confirming_state_and_timeout(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    from mcubridge.services.handshake import HandshakeState, SerialHandshakeManager

    test_config.serial_shared_secret = b"test_secret"
    mock_send = AsyncMock(return_value=True)
    hs = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=mock_send,
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    sync_attempt: Callable[[], Awaitable[bool]] = getattr(hs, "_synchronize_attempt")

    # 1. State not SYNCING before wait confirmation (line 211->214)
    mocker.patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True)
    hs.fsm_state = HandshakeState.CONFIRMING
    mocker.patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=True)
    assert await sync_attempt() is True

    # 2. Confirmed=False, pending_nonce == nonce (line 224->226)
    mocker.patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=False)
    hs.fsm_state = HandshakeState.SYNCING
    mock_fail = mocker.patch.object(hs, "handle_handshake_failure", new_callable=AsyncMock)
    assert await sync_attempt() is False
    assert mock_fail.called

    # 3. Confirmed=True, current_state == CONFIRMING (line 229->232)
    mocker.patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=True)
    hs.fsm_state = HandshakeState.CONFIRMING
    assert await sync_attempt() is True
    assert hs.fsm_state == HandshakeState.SYNCHRONIZED

    # 4. handle_link_sync_resp rate limit updated (line 261)
    test_config.serial_handshake_min_interval = 2.0
    mock_state.handshake_rate_until = 0.0
    nonce = b"valid_nonce_12"
    mock_state.link_handshake_nonce = nonce
    mock_state.link_expected_tag = hs.calculate_handshake_tag(test_config.serial_shared_secret, nonce)
    mocker.patch.object(hs, "_handle_handshake_success", new_callable=AsyncMock)
    mocker.patch.object(hs, "_fetch_capabilities_with_delay", new_callable=AsyncMock)
    res = await hs.handle_link_sync_resp(1, pb.LinkSync(nonce=nonce, tag=mock_state.link_expected_tag))
    assert res is True
    assert mock_state.handshake_rate_until > 0.0


@pytest.mark.asyncio
async def test_metrics_cancel_and_disabled_branches(mock_state: RuntimeState) -> None:
    import mcubridge.metrics as metrics_mod
    from mcubridge.metrics import publish_bridge_snapshots

    _build_metrics_message: Callable[..., pb.CloudQueuedPublish] = getattr(metrics_mod, "_build_metrics_message")
    _emit_bridge_snapshot: Callable[..., Awaitable[None]] = getattr(metrics_mod, "_emit_bridge_snapshot")

    # 1. _build_metrics_message without watchdog (line 69->74)
    snapshot = mock_state.build_metrics_snapshot()
    snapshot.watchdog_enabled = False
    msg = _build_metrics_message(mock_state, snapshot, expiry_seconds=10.0)
    assert msg.topic_name.endswith("metrics")

    # 2. _emit_bridge_snapshot CancelledError (line 108)
    mock_enq_cancel = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _emit_bridge_snapshot(mock_state, mock_enq_cancel, flavor="summary")

    # 3. publish_bridge_snapshots disabled (line 175)
    mock_enq = AsyncMock()
    task = asyncio.create_task(
        publish_bridge_snapshots(mock_state, mock_enq, summary_interval=0.0, handshake_interval=0.0)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_runtime_spool_and_pin_edge_branches(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. enqueue_cloud debug logging enabled (line 233)
    mocker.patch("structlog.stdlib.BoundLogger.is_enabled_for", return_value=True)
    mocker.patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True)
    await svc.enqueue_cloud(pb.CloudQueuedPublish(topic_name="test", payload=b"p"))

    # 2. _spool_cloud_message_locked when cloud_queue_limit == 0 (line 277->295)
    mock_state.cloud_queue_limit = 0
    mock_spool = AsyncMock()
    mock_spool.len.return_value = 100
    mock_spool.append.return_value = 0
    setattr(svc, "_cloud_spool", mock_spool)
    spool_locked: Callable[..., Awaitable[bool]] = getattr(svc, "_spool_cloud_message_locked")
    await spool_locked(pb.CloudQueuedPublish(topic_name="test", payload=b"p"))

    # 3. _flush_cloud_spool_locked when publish returns False (line 354)
    mock_spool.len.return_value = 1
    mock_spool.popleft.return_value = pb.CloudQueuedPublish(topic_name="test", payload=b"p")
    mocker.patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=False)
    flush_locked: Callable[[], Awaitable[None]] = getattr(svc, "_flush_cloud_spool_locked")
    await flush_locked()

    # 4. _handle_datastore PUT with cache active (line 800->802)
    mock_cache = AsyncMock()
    mock_state.datastore_cache = mock_cache
    route_put = TopicRoute(raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.PUT.value, "k1"))
    mocker.patch.object(svc, "publish_datastore_value", new_callable=AsyncMock)
    handle_ds: Callable[..., Awaitable[None]] = getattr(svc, "_handle_datastore")
    await handle_ds(route_put, pb.CloudQueuedPublish(payload=b"v1"))
    mock_cache.set.assert_called_once_with("k1", b"v1")


@pytest.mark.asyncio
async def test_serial_transport_additional_branches(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    transport = SerialTransport(test_config, mock_state, None)

    # 1. _correlate_frame with debug logging (line 249)
    mocker.patch("structlog.stdlib.BoundLogger.is_enabled_for", return_value=True)
    correlate_fn: Callable[[int, bytes], None] = getattr(transport, "_correlate_frame")
    correlate_fn(Status.ACK.value, b"")

    # 2. _negotiate_baudrate success future (line 424)
    transport.serial = AsyncMock()
    mocker.patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=True)

    async def _resolve_fut() -> None:
        await asyncio.sleep(0.01)
        fut = getattr(transport, "_negotiation_future")
        if fut:
            fut.set_result(True)

    asyncio.create_task(_resolve_fut())
    neg_fn: Callable[[int], Awaitable[bool]] = getattr(transport, "_negotiate_baudrate")
    res = await neg_fn(115200)
    assert res is True


@pytest.mark.asyncio
async def test_handshake_fetch_capabilities_with_delay_called(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    from mcubridge.services.handshake import SerialHandshakeManager

    AsyncMock(spec=SerialTransport)
    hs = SerialHandshakeManager(
        config=test_config,
        state=mock_state,
        serial_timing=pb.HandshakeConfig(),
        send_frame=AsyncMock(return_value=True),
        enqueue_cloud=AsyncMock(),
        acknowledge_frame=AsyncMock(),
    )
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)
    mock_fetch = mocker.patch.object(hs, "_fetch_capabilities", new_callable=AsyncMock)
    fetch_delay_fn: Callable[[], Awaitable[None]] = getattr(hs, "_fetch_capabilities_with_delay")
    await fetch_delay_fn()
    assert mock_fetch.called


def test_daemon_main_block_simulation(mocker: MockerFixture) -> None:
    from mcubridge import daemon

    mock_rd = mocker.patch.object(daemon, "run_daemon")
    daemon.app()
    assert mock_rd.called


@pytest.mark.asyncio
async def test_metrics_publisher_cancelled_tasks(mock_state: RuntimeState) -> None:
    from mcubridge.metrics import publish_bridge_snapshots, publish_metrics

    mock_enq = AsyncMock()

    # 1. publish_metrics cancellation
    t1 = asyncio.create_task(publish_metrics(mock_state, mock_enq, interval=0.1, min_interval=0.1))
    await asyncio.sleep(0.02)
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1

    # 2. publish_bridge_snapshots cancellation with active loops
    t2 = asyncio.create_task(
        publish_bridge_snapshots(mock_state, mock_enq, summary_interval=0.1, handshake_interval=0.1, min_interval=0.1)
    )
    await asyncio.sleep(0.02)
    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2


@pytest.mark.asyncio
async def test_serial_transport_run_loop_branches(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    mock_serial_inst = AsyncMock()
    mock_serial_inst.transport = MagicMock()

    # 1. service is None on connect/disconnect
    mock_serial_cls = mocker.patch("serialx.AsyncSerial")
    mock_serial_cls.return_value.__aenter__.return_value = mock_serial_inst

    async def _pending_read(_s: Any) -> None:
        await asyncio.Event().wait()

    mocker.patch.object(transport, "_toggle_dtr", new_callable=AsyncMock)
    mocker.patch.object(transport, "_read_loop", side_effect=_pending_read)
    stop_event: asyncio.Event = getattr(transport, "_stop_event")
    connect_and_run: Callable[[], Awaitable[None]] = getattr(transport, "_connect_and_run")
    stop_event.set()
    await connect_and_run()

    # 2. read_task finishes first -> raises ConnectionError
    stop_event.clear()
    mocker.patch.object(transport, "_read_loop", new_callable=AsyncMock, return_value=None)
    with pytest.raises(ConnectionError, match="Serial connection lost"):
        await connect_and_run()


@pytest.mark.asyncio
async def test_runtime_system_and_pin_edge_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.protocol.protocol import PinAction

    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    handle_sys: Callable[..., Awaitable[None]] = getattr(svc, "_handle_system")
    handle_spi: Callable[..., Awaitable[None]] = getattr(svc, "_handle_spi")
    handle_pin: Callable[..., Awaitable[None]] = getattr(svc, "_handle_pin")

    # 1. _handle_system with serial=None
    svc.serial = None
    route_sys = TopicRoute(raw="", prefix="bridge", topic=Topic.SYSTEM, segments=("version", "get"))
    await handle_sys(route_sys, pb.CloudQueuedPublish())
    assert not serial.send.called

    # 2. _handle_system with unknown action
    svc.serial = serial
    route_sys_unknown = TopicRoute(raw="", prefix="bridge", topic=Topic.SYSTEM, segments=("unknown_sys_action",))
    await handle_sys(route_sys_unknown, pb.CloudQueuedPublish())
    assert not serial.send.called

    # 3. _handle_spi with unknown identifier
    route_spi_unknown = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("unknown_spi_action",))
    await handle_spi(route_spi_unknown, pb.CloudQueuedPublish())
    assert not serial.send.called

    # 4. _handle_pin mode set
    route_pin_mode = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13", PinAction.MODE.value))
    await handle_pin(route_pin_mode, pb.CloudQueuedPublish(payload=b"1"))
    assert serial.send.called

    # 5. _handle_pin analog write
    serial.send.reset_mock()
    route_pin_aw = TopicRoute(raw="", prefix="bridge", topic=Topic.ANALOG, segments=("3",))
    await handle_pin(route_pin_aw, pb.CloudQueuedPublish(payload=b"128"))
    assert serial.send.called


@pytest.mark.asyncio
async def test_runtime_inbound_unhandled_topic(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    req = pb.CloudQueuedPublish(topic_name="bridge/status/unhandled", payload=b"test")
    await svc.handle_request(req)
    assert not serial.send.called


@pytest.mark.asyncio
async def test_runtime_shell_properties_content_type(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    mock_props = MagicMock()
    mock_props.ContentType = PROTOBUF_CONTENT_TYPE
    inbound = MagicMock()
    inbound.content_type = None
    inbound.properties = mock_props
    inbound.payload = pb.ProcessRunAsync(command="echo prop").SerializeToString()

    async def _mock_run_p(_cmd: str) -> int:
        return 42

    async def _mock_enq(_msg: Any, **_kwargs: Any) -> bool:
        return True

    mock_rp = mocker.patch.object(svc, "run_process", side_effect=_mock_run_p)
    mock_eq = mocker.patch.object(svc, "enqueue_cloud", side_effect=_mock_enq)
    h_run_async: Callable[..., Awaitable[None]] = getattr(svc, "_handle_shell_run_async")
    await h_run_async(0, inbound)
    assert mock_rp.called
    assert mock_eq.called


@pytest.mark.asyncio
async def test_runtime_run_process_with_task_group(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    async def _mock_mon(_pid: int) -> None:
        pass

    def _mock_create_task(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()

    mock_tg = MagicMock()
    mock_tg.create_task.side_effect = _mock_create_task
    setattr(svc, "_tg", mock_tg)
    mocker.patch.object(svc, "_monitor_process", side_effect=_mock_mon)
    mock_exec = mocker.patch("asyncio.create_subprocess_exec")
    mock_p = MagicMock()
    mock_p.pid = 456
    mock_exec.return_value = mock_p
    pid = await svc.run_process("echo tg")
    assert pid == 456
    assert mock_tg.create_task.called
    setattr(svc, "_tg", None)


@pytest.mark.asyncio
async def test_runtime_run_cloud_cancelled(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    async def _mock_cloud_cancel(_tls: Any) -> None:
        raise asyncio.CancelledError()

    mocker.patch.object(svc, "connect_cloud_session", side_effect=_mock_cloud_cancel)
    with pytest.raises(asyncio.CancelledError):
        await svc.run_cloud()


def test_protocol_frame_validation_error_paths() -> None:
    from mcubridge.protocol import frame, protocol

    with pytest.raises(ValueError, match="Invalid command ID"):
        frame.build_frame(-1, 1)

    with pytest.raises(ValueError, match="Invalid command ID"):
        frame.build_frame(protocol.UINT16_MAX + 1, 1)

    with pytest.raises(ValueError, match="Invalid sequence ID"):
        frame.build_frame(1, -1)

    with pytest.raises(ValueError, match="Invalid sequence ID"):
        frame.build_frame(1, protocol.UINT16_MAX + 1)

    valid_frame = frame.build_frame(1, 1)
    # Tamper with version field
    bytearray(valid_frame)
    # Repack with invalid version
    from mcubridge.protocol import mcubridge_pb2 as pb
    from binascii import crc32
    import struct

    env = pb.RpcEnvelope(version=99, command_id=1, sequence_id=1)
    body = env.SerializeToString()
    bad_ver_frame = body + struct.pack("<I", crc32(body) & protocol.CRC32_MASK)
    with pytest.raises(ValueError, match="Unsupported protocol version"):
        frame.parse_frame(bad_ver_frame)


@pytest.mark.asyncio
async def test_runtime_service_spi_and_system_branches(runtime_config: Any, runtime_state: Any) -> None:
    from mcubridge.protocol.topics import parse_topic
    from mcubridge.protocol import mcubridge_pb2 as pb
    from mcubridge.services.runtime import BridgeService
    from unittest.mock import AsyncMock

    serial = AsyncMock()
    service = BridgeService(runtime_config, runtime_state, serial)
    handle_spi: Callable[..., Awaitable[None]] = getattr(service, "_handle_spi")
    handle_sys: Callable[..., Awaitable[None]] = getattr(service, "_handle_system")
    handle_pin: Callable[..., Awaitable[None]] = getattr(service, "_handle_pin")
    pub_cloud: Callable[..., Awaitable[bool]] = getattr(service, "_publish_cloud_message")

    # SPI begin & end
    route_begin = parse_topic("br", "br/spi/begin")
    assert route_begin is not None
    await handle_spi(route_begin, pb.CloudQueuedPublish(payload=b""))
    serial.send.assert_awaited()

    route_end = parse_topic("br", "br/spi/end")
    assert route_end is not None
    await handle_spi(route_end, pb.CloudQueuedPublish(payload=b""))
    serial.send.assert_awaited()

    # SPI config (valid & invalid)
    route_cfg = parse_topic("br", "br/spi/config")
    assert route_cfg is not None
    cfg_pb = pb.SpiConfig(frequency=4000000, bit_order=1, data_mode=0)
    await handle_spi(route_cfg, pb.CloudQueuedPublish(payload=cfg_pb.SerializeToString()))
    await handle_spi(route_cfg, pb.CloudQueuedPublish(payload=b"\xff\xff\xff\xff"))

    # SPI transfer (empty payload, non-bytes response, and populated with bytes response)
    route_tr = parse_topic("br", "br/spi/transfer")
    assert route_tr is not None
    await handle_spi(route_tr, pb.CloudQueuedPublish(payload=b""))

    serial.send.return_value = False
    await handle_spi(route_tr, pb.CloudQueuedPublish(payload=b"\x01\x02"))

    resp_payload = pb.SpiTransferResponse(data=b"\xde\xad\xbe\xef").SerializeToString()
    serial.send.return_value = resp_payload
    await handle_spi(route_tr, pb.CloudQueuedPublish(payload=b"\x01\x02\x03\x04"))

    # System bootloader
    route_boot = parse_topic("br", "br/system/bootloader")
    assert route_boot is not None
    await handle_sys(route_boot, pb.CloudQueuedPublish())

    # System free memory (non-bytes and bytes response)
    route_mem = parse_topic("br", "br/system/free_memory/get")
    assert route_mem is not None
    serial.send.return_value = False
    await handle_sys(route_mem, pb.CloudQueuedPublish())
    serial.send.return_value = pb.FreeMemoryResponse(value=1024).SerializeToString()
    await handle_sys(route_mem, pb.CloudQueuedPublish())

    # System version
    route_ver = parse_topic("br", "br/system/version/get")
    assert route_ver is not None
    serial.send.return_value = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    await handle_sys(route_ver, pb.CloudQueuedPublish())

    # System bridge snapshots (summary & handshake)
    route_snap = parse_topic("br", "br/system/bridge/summary")
    assert route_snap is not None
    await handle_sys(route_snap, pb.CloudQueuedPublish())

    route_hs = parse_topic("br", "br/system/bridge/handshake")
    assert route_hs is not None
    await handle_sys(route_hs, pb.CloudQueuedPublish())

    # Serial is None paths & unknown action paths
    service.serial = None
    await handle_spi(route_begin, pb.CloudQueuedPublish())
    await handle_sys(route_boot, pb.CloudQueuedPublish())
    await handle_pin(route_boot, pb.CloudQueuedPublish())

    service.serial = serial
    unknown_route = parse_topic("br", "br/system/nonexistent_action")
    if unknown_route:
        await handle_sys(unknown_route, pb.CloudQueuedPublish())

    # Cloud publish branches
    assert await pub_cloud(pb.CloudQueuedPublish()) is False
    mock_stream_pub = AsyncMock()
    setattr(service, "_cloud_stream", mock_stream_pub)
    for topic_sample in ["br/metrics/report", "br/summary/report", "br/handshake/report", "br/status/report"]:
        assert await pub_cloud(pb.CloudQueuedPublish(topic_name=topic_sample, payload=b"test")) is True

    assert (
        await pub_cloud(pb.CloudQueuedPublish(correlation_data=b"\x00\x00\x00\x00\x00\x00\x00\x01", payload=b"ok"))
        is True
    )

    mock_stream_pub.send_message.side_effect = OSError("network down")
    assert await pub_cloud(pb.CloudQueuedPublish(topic_name="br/metrics/report")) is False


@pytest.mark.asyncio
async def test_runtime_reset_link_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(test_config, mock_state, serial)

    # 1. reset_link with active serial
    res_true = await service.reset_link()
    assert res_true is True
    serial.reset.assert_awaited_once()

    # 2. reset_link without serial
    service.serial = None
    res_false = await service.reset_link()
    assert res_false is False
    service.cleanup()


def test_parse_serial_response_corrupt_bytes() -> None:
    from mcubridge.services.local_bridge import parse_serial_response

    default_resp = pb.GenericResponse(message="fallback")
    parsed = parse_serial_response(b"\xff\xff\xff", pb.GenericResponse, default_resp)
    assert parsed == default_resp
    assert parsed.message == "fallback"


@pytest.mark.asyncio
async def test_on_mcu_file_read_resp_future_already_done(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.services.runtime import _PendingMcuRead

    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(test_config, mock_state, serial)

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result(b"prior_result")
    pending = _PendingMcuRead(fut)
    pending.chunks = [b"prior_result"]
    setattr(service, "_pending_mcu_read", pending)

    on_read_resp: Callable[..., Awaitable[bool]] = getattr(service, "_on_mcu_file_read_resp")
    res = await on_read_resp(1, pb.FileReadResponse(content=b""))
    assert res is True
    assert fut.result() == b"prior_result"
    service.cleanup()


@pytest.mark.asyncio
async def test_handle_datastore_cache_none_and_exceptions(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    from mcubridge.protocol.topics import parse_topic

    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(test_config, mock_state, serial)
    service.publish_datastore_value = AsyncMock()
    handle_ds: Callable[..., Awaitable[None]] = getattr(service, "_handle_datastore")

    # 1. Datastore PUT with cache = None
    service.state.datastore_cache = None
    route = parse_topic("br", "br/datastore/put/settings/theme")
    assert route is not None
    inbound = pb.CloudQueuedPublish(payload=b"dark")
    await handle_ds(route, inbound)
    service.publish_datastore_value.assert_awaited_once_with("settings/theme", b"dark", reply_context=inbound)

    # 2. Datastore PUT instantiation error
    mocker.patch("mcubridge.protocol.mcubridge_pb2.DatastorePut", side_effect=TypeError("malformed"))
    await handle_ds(route, inbound)

    service.cleanup()


@pytest.mark.asyncio
async def test_send_cloud_event_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(test_config, mock_state, serial)
    send_cloud_event: Callable[..., Awaitable[None]] = getattr(service, "_send_cloud_event")

    # 1. Without cloud stream (no-op)
    setattr(service, "_cloud_stream", None)
    await send_cloud_event("heartbeat", "info", "test_msg")

    # 2. With active cloud stream
    mock_stream = AsyncMock()
    setattr(service, "_cloud_stream", mock_stream)
    await send_cloud_event("heartbeat", "info", "test_msg")
    mock_stream.send_message.assert_awaited_once()
    envelope = mock_stream.send_message.call_args[0][0]
    assert envelope.event.event_type == "heartbeat"
    assert envelope.event.description == "test_msg"

    service.cleanup()


def test_structures_tls_session_ticket_exceptions() -> None:
    from mcubridge.protocol.structures import save_tls_session_ticket, load_tls_session_ticket

    # 1. persist with txn.put raising OSError
    mock_env = MagicMock()
    mock_txn = MagicMock()
    mock_txn.put.side_effect = OSError("disk full")
    mock_env.begin.return_value.__enter__.return_value = mock_txn
    cache = MagicMock()
    cache.env = mock_env
    cache.db = MagicMock()
    save_tls_session_ticket(cache, "example.com", 443, b"ticket-data")
    mock_txn.put.assert_called_once()

    # 2. load with txn.get raising RuntimeError
    del cache._mem
    mock_txn.get.side_effect = RuntimeError("lmdb read failure")
    res = load_tls_session_ticket(cache, "example.com", 443)
    assert res is None


def test_serial_safe_after_configure_branches(mocker: MockerFixture) -> None:
    import errno
    import termios
    import mcubridge.transport.serial as serial_mod

    _safe_after_configure: Callable[[Any], None] = getattr(serial_mod, "_safe_after_configure")

    # 1. Handled errno (e.g. EINVAL) and _fileno is None
    mock_self = MagicMock()
    mock_self._fileno = None
    mock_orig = mocker.patch(
        "mcubridge.transport.serial._orig_after_configure", side_effect=OSError(errno.EINVAL, "Invalid argument")
    )
    _safe_after_configure(mock_self)

    # 2. Unhandled errno (e.g. EACCES) -> must raise
    mock_orig.side_effect = OSError(errno.EACCES, "Permission denied")
    with pytest.raises(OSError):
        _safe_after_configure(mock_self)

    # 3. Fileno is not None, but tcgetattr raises termios.error
    mock_orig.side_effect = None
    mock_orig.return_value = None
    mock_self._fileno = 42
    mocker.patch("termios.tcgetattr", side_effect=termios.error("mock termios failure"))
    _safe_after_configure(mock_self)
    assert mock_self._fileno == 42


@pytest.mark.asyncio
async def test_runtime_shell_run_async_invalid_payload(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.services.runtime import BridgeService

    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(test_config, mock_state, serial)
    service.enqueue_cloud = AsyncMock()

    # Payload with protobuf marker but corrupted content
    inbound = pb.CloudQueuedPublish(payload=b"\x0a\xff\xff\xff", content_type="application/x-protobuf")
    h_run_async: Callable[..., Awaitable[None]] = getattr(service, "_handle_shell_run_async")
    await h_run_async(1, inbound)

    service.enqueue_cloud.assert_awaited_once()
    msg = service.enqueue_cloud.call_args[0][0]
    resp = pb.ProcessRunAsyncResponse.FromString(msg.payload)
    assert resp.pid == 0

    service.cleanup()


@pytest.mark.asyncio
async def test_serial_read_loop_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.protocol.protocol import FRAME_DELIMITER

    transport = SerialTransport(test_config, mock_state, AsyncMock())
    mock_serial = AsyncMock()

    # 1. Stop event set before loop starts -> immediately exits
    stop_event: asyncio.Event = getattr(transport, "_stop_event")
    stop_event.set()
    read_loop: Callable[..., Awaitable[None]] = getattr(transport, "_read_loop")
    await read_loop(mock_serial)
    mock_serial.readuntil.assert_not_called()

    # 2. Empty packet (delimiter only) -> packet_view is empty, does not call process_packet
    stop_event.clear()
    mock_serial.readuntil.side_effect = [
        FRAME_DELIMITER,  # Empty frame (len 1, view len 0)
        asyncio.CancelledError(),
    ]
    mock_proc = AsyncMock()
    setattr(transport, "_process_packet", mock_proc)
    with pytest.raises(asyncio.CancelledError):
        await read_loop(mock_serial)

    mock_proc.assert_not_awaited()
    assert getattr(transport, "_consecutive_crc_errors") == 0


# ==========================================
# 11. Pure Branch Coverage Hardening (>= 95%)
# ==========================================


@pytest.mark.asyncio
async def test_watchdog_uncovered_branch_hardening(mock_state: RuntimeState, mocker: MockerFixture) -> None:
    from mcubridge.watchdog import WatchdogKeepalive

    # 1. is_healthy() when fatal_count > 0 and not critical_inhibit (lines 102-104)
    wd = WatchdogKeepalive(interval=10.0, state=mock_state)
    setattr(mock_state, "fatal_count", 1)
    assert wd.is_healthy() is False
    assert wd.fsm.critical_inhibit.is_active

    # 2. trip_inhibit() when already in critical_inhibit (line 109->exit)
    wd.trip_inhibit("second attempt")
    assert wd.fsm.critical_inhibit.is_active

    # 3. degrade() when in critical_inhibit (line 115->exit)
    wd.degrade("attempt while inhibited")
    assert wd.fsm.critical_inhibit.is_active

    # 4. recover() when in critical_inhibit (line 121->exit)
    wd.recover()
    assert wd.fsm.critical_inhibit.is_active

    # 5. kick() when supervisor recovers from degraded (line 140->141)
    setattr(mock_state, "fatal_count", 0)
    wd2 = WatchdogKeepalive(interval=10.0, state=mock_state)
    wd2.fsm.start_healthy()
    wd2.fsm.degrade()
    assert wd2.fsm.degraded.is_active
    setattr(wd2, "_token", b"W")
    mocker.patch.object(wd2, "_write")
    wd2.kick()
    assert wd2.fsm.healthy.is_active

    # 6. run() when shutdown is already active (line 153->exit)
    wd3 = WatchdogKeepalive(interval=10.0, state=mock_state)
    wd3.fsm.stop()
    assert wd3.fsm.shutdown.is_active
    await wd3.run()


def test_structures_uncovered_branch_hardening(test_config: RuntimeConfig) -> None:
    from mcubridge.protocol.structures import (
        load_tls_session_ticket,
        save_tls_session_ticket,
        validate_config,
    )

    # 1. validate_config when cloud_http3_port is 0 (line 156->159)
    cfg = pb.RuntimeConfig()
    cfg.CopyFrom(test_config)
    cfg.cloud_http3_port = 0
    validate_config(cfg)
    assert cfg.cloud_http3_port == 0

    # 2. save_tls_session_ticket when cache has no _mem (line 210->212)
    mock_cache = MagicMock(spec=["env", "db"])
    mock_txn = MagicMock()
    mock_cache.env.begin.return_value.__enter__.return_value = mock_txn
    save_tls_session_ticket(mock_cache, "example.com", 443, b"ticket_data")
    mock_txn.put.assert_called_once()

    # 3. load_tls_session_ticket when env is None and _mem present (lines 227->235, 236)
    cache_mem_only = MagicMock(spec=["env", "_mem", "is_mem"])
    cache_mem_only.env = None
    cache_mem_only.is_mem = False
    cache_mem_only._mem = {"tls_ticket:example.com:443": b"cached_ticket"}
    res1 = load_tls_session_ticket(cache_mem_only, "example.com", 443)
    assert res1 == b"cached_ticket"

    # 4. load_tls_session_ticket when neither env nor _mem present (lines 235->237)
    cache_empty = MagicMock(spec=[])
    res2 = load_tls_session_ticket(cache_empty, "example.com", 443)
    assert res2 is None


@pytest.mark.asyncio
async def test_metrics_uncovered_branch_hardening(mock_state: RuntimeState) -> None:
    from mcubridge.config import const
    import mcubridge.metrics as metrics_mod
    from mcubridge.metrics import publish_bridge_snapshots

    _build_metrics_message: Callable[..., pb.CloudQueuedPublish] = getattr(metrics_mod, "_build_metrics_message")

    # 1. _build_metrics_message when storage_rejections==0 and write_rejections>0 (line 53)
    mock_state.file_storage_limit_rejections = 0
    mock_state.file_write_limit_rejections = 3
    snap = mock_state.build_metrics_snapshot()
    msg = _build_metrics_message(mock_state, snap, expiry_seconds=10.0)
    assert any(
        prop.key == const.PROP_KEY_BRIDGE_FILES and prop.value == const.PROP_VAL_WRITE_LIMIT
        for prop in msg.user_properties
    )

    # 2. publish_bridge_snapshots when summary_interval > 0 and handshake_interval == 0 (line 179->exit)
    mock_enq1 = AsyncMock()
    t1 = asyncio.create_task(
        publish_bridge_snapshots(mock_state, mock_enq1, summary_interval=0.1, handshake_interval=0.0, min_interval=0.1)
    )
    await asyncio.sleep(0.02)
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1

    # 3. publish_bridge_snapshots when summary_interval == 0 and handshake_interval > 0 (line 165->179)
    mock_enq2 = AsyncMock()
    t2 = asyncio.create_task(
        publish_bridge_snapshots(mock_state, mock_enq2, summary_interval=0.0, handshake_interval=0.1, min_interval=0.1)
    )
    await asyncio.sleep(0.02)
    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2


def test_state_context_uncovered_branch_hardening(test_config: RuntimeConfig) -> None:
    # 1. on_enter_connected when serial_tx_allowed is None (line 509->exit)
    st = create_runtime_state(test_config)
    setattr(st, "serial_tx_allowed", None)
    st.connection_fsm.connect()
    assert st.is_connected

    # 2. configure when resource has no close() method (line 569->exit)
    st2 = create_runtime_state(test_config)
    st2.mailbox_queue = cast(Any, object())
    st2.configure()
    assert st2.mailbox_queue is not None

    # 3. build_status_snapshot process branches (lines 737->736, 739->736)
    st3 = create_runtime_state(test_config)
    mock_proc1 = MagicMock(spec=asyncio.subprocess.Process)
    ctx1 = ProcessContext(mock_proc1)
    setattr(ctx1, "handle", None)
    st3.running_processes[1] = ctx1

    mock_proc2 = MagicMock(spec=asyncio.subprocess.Process)
    mock_proc2.pid = 99999999
    ctx2 = ProcessContext(mock_proc2)
    st3.running_processes[2] = ctx2

    snap3 = st3.build_status_snapshot()
    assert len(snap3.process_stats) >= 1


@pytest.mark.asyncio
async def test_lmdb_cache_uncovered_branches() -> None:
    from mcubridge.state.storage import LmdbCache
    import lmdb

    cache = LmdbCache(path="/tmp/test_cache_branches.db")
    cache.env = None
    assert len(cache) == 0
    assert await cache.contains("k") is False
    assert ("k" in cache) is False
    assert await cache.items() == []

    # Exception branches
    cache.env = MagicMock()
    cache.env.begin.side_effect = lmdb.Error("forced")
    assert len(cache) == 0
    assert await cache.contains("k") is False
    assert ("k" in cache) is False
    assert await cache.items() == []
    assert await cache.get("k", b"def") == b"def"
    assert await cache.pop("k", b"def") == b"def"


def test_tls_session_ticket_uncovered_branches() -> None:
    import ssl
    from mcubridge.protocol.structures import (
        get_ssl_context,
        load_tls_session_ticket,
        save_tls_session_ticket,
    )

    # 1. SSL context insecure (lines 183-184)
    cfg_insecure = pb.RuntimeConfig(cloud_tls=True, cloud_tls_insecure=True)
    ctx = get_ssl_context(cfg_insecure)
    assert ctx is not None
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE

    # 2. cache is None or empty ticket
    save_tls_session_ticket(None, "host", 443, b"ticket")
    save_tls_session_ticket(object(), "host", 443, b"")
    assert load_tls_session_ticket(None, "host", 443) is None

    # 3. is_mem branch
    mem_cache = MagicMock()
    mem_cache.is_mem = True
    mem_cache._mem = {}
    save_tls_session_ticket(mem_cache, "host", 443, b"ticket")
    assert load_tls_session_ticket(mem_cache, "host", 443) == b"ticket"

    # 4. disk_cache exception branches (lines 216-217, 232-234)
    disk_cache = MagicMock()
    disk_cache.is_mem = False
    disk_cache.env = MagicMock()
    disk_cache.db = MagicMock()
    disk_cache.env.begin.side_effect = OSError("write error")
    save_tls_session_ticket(disk_cache, "host", 443, b"ticket")
    assert load_tls_session_ticket(disk_cache, "host", 443) is None


def test_daemon_uncovered_branches(mocker: MockerFixture) -> None:
    from mcubridge.daemon import run_daemon

    # 1. unhandled in ExceptionGroup
    mocker.patch("mcubridge.daemon.load_runtime_config")
    mocker.patch("mcubridge.daemon.configure_logging")
    mocker.patch("mcubridge.daemon.verify_crypto_integrity", return_value=True)
    mocker.patch(
        "mcubridge.daemon.create_runtime_state",
        side_effect=ExceptionGroup("eg", [ZeroDivisionError("unhandled")]),
    )
    with pytest.raises(ExceptionGroup):
        run_daemon()

    # 2. SerialTransport creation failure triggers state.cleanup() without service (lines 120-121)
    mocker.patch("mcubridge.daemon.SerialTransport", side_effect=RuntimeError("transport init error"))
    mocker.patch("mcubridge.daemon.create_runtime_state")
    with pytest.raises(SystemExit) as exc_info:
        run_daemon()
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_handshake_uncovered_sync_branches(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(test_config, mock_state, serial)
    mgr = service.handshake

    # 1. handle_link_sync_resp when link_handshake_nonce is None (line 342)
    mock_state.link_handshake_nonce = None
    res = await mgr.handle_link_sync_resp(1, b"nonce")
    assert res is False

    # 2. _publish_handshake_event when topic_name is empty (lines 603-607)
    mocker.patch("mcubridge.services.handshake.get_topic_for_message", return_value="")
    mock_enqueue = AsyncMock()
    setattr(mgr, "_enqueue_cloud", mock_enqueue)
    pub_event_fn: Callable[..., Awaitable[None]] = getattr(mgr, "_publish_handshake_event")
    await pub_event_fn("custom_event")
    assert not mock_enqueue.called
    service.cleanup()


@pytest.mark.asyncio
async def test_runtime_flush_cloud_spool_uncovered_branches(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    from mcubridge.state.storage import LmdbDeque
    import lmdb

    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    setattr(svc, "_cloud_stream", MagicMock())

    # 1. _publish_cloud_message returns False (line 435)
    mock_spool = MagicMock(spec=LmdbDeque)
    setattr(svc, "_cloud_spool", mock_spool)
    mock_spool.__len__.return_value = 1
    mock_spool.peek = AsyncMock(return_value=pb.CloudQueuedPublish(topic_name="t").SerializeToString())
    setattr(svc, "_publish_cloud_message", AsyncMock(return_value=False))
    flush_fn: Callable[[], Awaitable[None]] = getattr(svc, "_flush_cloud_spool_locked")
    await flush_fn()

    # 2. spool.popleft raises IndexError (lines 439-441)
    setattr(svc, "_publish_cloud_message", AsyncMock(return_value=True))
    mock_spool.popleft = AsyncMock(side_effect=IndexError("empty"))
    await flush_fn()

    # 3. spool.popleft raises lmdb.Error (lines 442-445)
    mock_spool.popleft = AsyncMock(side_effect=lmdb.Error("db error"))
    await flush_fn()
    assert mock_state.cloud_spool_degraded is True
    svc.cleanup()


@pytest.mark.asyncio
async def test_handshake_sync_send_frame_failure(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(test_config, mock_state, serial)
    mgr = service.handshake

    async def _mock_send(cmd: int, payload: Any) -> bool:
        if cmd == Command.CMD_LINK_RESET.value:
            return True
        return False

    setattr(mgr, "_send_frame", _mock_send)
    synchronize_attempt_fn: Callable[[], Awaitable[bool]] = getattr(mgr, "_synchronize_attempt")
    res = await synchronize_attempt_fn()
    assert res is False
    service.cleanup()


def test_serial_safe_after_configure_exceptions() -> None:
    import errno
    import mcubridge.transport.serial as serial_mod

    safe_fn: Callable[[Any], None] = getattr(serial_mod, "_safe_after_configure")

    # 1. OSError with EINVAL ignored
    mock_obj = MagicMock()
    mock_obj._fileno = None
    orig_fn = getattr(serial_mod, "_orig_after_configure")
    try:
        setattr(serial_mod, "_orig_after_configure", MagicMock(side_effect=OSError(errno.EINVAL, "Invalid")))
        safe_fn(mock_obj)

        # 2. OSError with unhandled errno raised
        setattr(serial_mod, "_orig_after_configure", MagicMock(side_effect=OSError(errno.EACCES, "Denied")))
        with pytest.raises(OSError):
            safe_fn(mock_obj)

        # 3. termios.tcgetattr raises termios.error
        setattr(serial_mod, "_orig_after_configure", None)
        mock_obj._fileno = 99
        import termios

        orig_tcgetattr = termios.tcgetattr
        orig_tcsetattr = termios.tcsetattr
        try:
            termios.tcgetattr = MagicMock(side_effect=termios.error("ioctl error"))
            safe_fn(mock_obj)

            # 4. termios success path (lines 79-81)
            attrs = [[], [], [], [], [], [], [0] * 32]
            termios.tcgetattr = MagicMock(return_value=attrs)
            mock_tcsetattr = MagicMock()
            termios.tcsetattr = mock_tcsetattr
            safe_fn(mock_obj)
            mock_tcsetattr.assert_called_once()
        finally:
            termios.tcgetattr = orig_tcgetattr
            termios.tcsetattr = orig_tcsetattr
    finally:
        setattr(serial_mod, "_orig_after_configure", orig_fn)


@pytest.mark.asyncio
async def test_handshake_synchronize_retry_error(
    test_config: RuntimeConfig, mock_state: RuntimeState, mocker: MockerFixture
) -> None:
    import tenacity
    from mcubridge.services.handshake import HandshakeState

    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(test_config, mock_state, serial)
    mgr = service.handshake

    fake_retryer = AsyncMock()
    fake_retryer.side_effect = tenacity.RetryError(MagicMock())
    mocker.patch("tenacity.AsyncRetrying", return_value=fake_retryer)

    res = await mgr.synchronize()
    assert res is False
    assert mgr.fsm_state == HandshakeState.FAULT
    service.cleanup()


@pytest.mark.asyncio
async def test_handshake_sync_fault_race_and_nonce_mismatch(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    from mcubridge.services.handshake import HandshakeEvent

    serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(test_config, mock_state, serial)
    mgr = service.handshake

    # 1. Race condition guard: fsm_state == FAULT right after sending LINK_SYNC (lines 311-312)
    async def _send_and_fault(cmd: int, payload: Any) -> bool:
        if cmd == Command.CMD_LINK_SYNC.value:
            mgr.transition(HandshakeEvent.FAILURE)
        return True

    setattr(mgr, "_send_frame", _send_and_fault)
    synchronize_attempt_fn: Callable[[], Awaitable[bool]] = getattr(mgr, "_synchronize_attempt")
    res1 = await synchronize_attempt_fn()
    assert res1 is False

    # 2. Confirmed is False and current_state == FAULT (lines 324-325)
    mgr.transition(HandshakeEvent.RESET)

    async def _send_ok(cmd: int, payload: Any) -> bool:
        return True

    async def _wait_fault(nonce: bytes) -> bool:
        mgr.transition(HandshakeEvent.FAILURE)
        return False

    setattr(mgr, "_send_frame", _send_ok)
    setattr(mgr, "_wait_for_link_sync_confirmation", _wait_fault)
    res2 = await synchronize_attempt_fn()
    assert res2 is False

    # 3. Confirmed is False, pending_nonce != nonce (line 329->exit)
    mgr.transition(HandshakeEvent.RESET)

    async def _wait_mismatch(nonce: bytes) -> bool:
        mock_state.link_handshake_nonce = b"different_nonce_to_trigger_exit"
        return False

    setattr(mgr, "_wait_for_link_sync_confirmation", _wait_mismatch)
    res3 = await synchronize_attempt_fn()
    assert res3 is False

    service.cleanup()
