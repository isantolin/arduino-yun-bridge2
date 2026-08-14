# pyright: reportPrivateUsage=false
"""Targeted branch coverage tests to push pure branch coverage above 95%."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge.config.logging import configure_logging
from mcubridge.config.settings import (
    RuntimeConfig,
    _coerce_value,
    _load_raw_config,
    _runtime_config_factory,
    load_runtime_config_from_json,
)
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.frame import build_frame
from mcubridge.protocol.protocol import (
    Command,
    DatastoreAction,
    ShellAction,
    SpiAction,
    Status,
    SystemAction,
    Topic,
)
from mcubridge.protocol.structures import (
    PendingCommand,
    TopicRoute,
)
from mcubridge.services.runtime import BridgeService, ProcessContext
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport
from mcubridge_client.definitions import build_bridge_args
from mcubridge_client.spi import SpiDevice


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
        metrics_enabled=False,
        watchdog_enabled=False,
    )


@pytest.fixture
def mock_state(test_config: RuntimeConfig) -> RuntimeState:
    return create_runtime_state(test_config)


# ==========================================
# 1. Config Settings & Logging Branches
# ==========================================


def test_settings_factory_bypass_defaults() -> None:
    with patch("mcubridge.config.settings.validate_config"):
        cfg = _runtime_config_factory(
            bypass_defaults=True,
            serial_shared_secret="secretstring",
            serial_port="/dev/ttyS0",
            serial_baud=115200,
            serial_safe_baud=115200,
        )
        assert cfg.serial_port == "/dev/ttyS0"
        assert isinstance(cfg.serial_shared_secret, bytes)


def test_settings_load_raw_config_empty_uci() -> None:
    with patch("mcubridge.config.settings.get_uci_config", return_value={}):
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


def test_settings_coerce_value_types() -> None:
    assert _coerce_value(None, None) is None

    field_bool = pb.RuntimeConfig.DESCRIPTOR.fields_by_name["cloud_enabled"]
    assert _coerce_value(True, field_bool, "cloud_enabled") is True
    assert _coerce_value("true", field_bool, "cloud_enabled") is True
    assert _coerce_value("1", field_bool, "cloud_enabled") is True
    assert _coerce_value("false", field_bool, "cloud_enabled") is False
    assert _coerce_value("invalid_val", field_bool, "cloud_enabled") is False

    field_int = pb.RuntimeConfig.DESCRIPTOR.fields_by_name["serial_baud"]
    assert _coerce_value("115200", field_int, "serial_baud") == 115200
    assert _coerce_value("invalid_int", field_int, "serial_baud") == 0

    field_float = pb.RuntimeConfig.DESCRIPTOR.fields_by_name["bridge_summary_interval"]
    assert _coerce_value("1.5", field_float, "bridge_summary_interval") == 1.5
    assert _coerce_value("invalid_float", field_float, "bridge_summary_interval") == 0.0

    field_str = pb.RuntimeConfig.DESCRIPTOR.fields_by_name["topic_prefix"]
    assert _coerce_value("   ", field_str, "topic_prefix") is None

    field_bytes = pb.RuntimeConfig.DESCRIPTOR.fields_by_name["serial_shared_secret"]
    assert _coerce_value(b"secret", field_bytes, "serial_shared_secret") == b"secret"
    assert _coerce_value("secret", field_bytes, "serial_shared_secret") == b"secret"

    class DummyField:
        pass

    assert _coerce_value("custom_val", DummyField(), "custom") == "custom_val"


def test_logging_discover_syslog_var_run_branch(test_config: RuntimeConfig) -> None:
    def _mock_exists(path_obj: Path) -> bool:
        return str(path_obj) == "/var/run/log"

    with patch.dict("os.environ", {}, clear=True):
        with patch.object(Path, "exists", _mock_exists):
            with patch("logging.handlers.SysLogHandler"):
                configure_logging(test_config)


# ==========================================
# 2. Context & Lifecycle Branches
# ==========================================


def test_context_mark_states_without_link_sync_event(test_config: RuntimeConfig) -> None:
    state = create_runtime_state(test_config)
    setattr(state, "link_sync_event", None)

    state.mark_transport_disconnected()
    assert state.state == "disconnected"

    state.mark_synchronized()
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
    transport._switch_local_baudrate(115200)

    # 2. reset() when _current is None
    transport._current = None
    await transport.reset()

    # 3. _toggle_dtr when serial is None
    await transport._toggle_dtr()

    # 4. stop() when serial is None
    await transport.stop()

    # 5. _check_baudrate_fallback when baud == safe_baud
    test_config.serial_baud = test_config.serial_safe_baud
    transport._consecutive_crc_errors = test_config.serial_fallback_threshold - 1
    await transport._check_baudrate_fallback()


@pytest.mark.asyncio
async def test_serial_transport_correlate_frame_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)

    # 1. ACK with non-matching ack_target
    transport._current = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    ack_pkt = pb.AckPacket(command_id=Command.CMD_ANALOG_WRITE.value)
    transport._correlate_frame(Status.ACK.value, ack_pkt.SerializeToString())
    assert transport._current.ack_received is False

    # 2. ACK with matching ack_target but non-empty expected_resp_ids
    transport._current = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    ack_matching = pb.AckPacket(command_id=Command.CMD_DIGITAL_WRITE.value)
    transport._correlate_frame(Status.ACK.value, ack_matching.SerializeToString())
    assert transport._current.ack_received is True
    assert transport._current.success is None

    # 3. Status in SERIAL_SUCCESS_STATUS_CODES with non-empty expected_resp_ids
    transport._current = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[Command.CMD_DIGITAL_WRITE.value],
    )
    transport._correlate_frame(Status.OK.value, b"")
    assert transport._current.success is None


@pytest.mark.asyncio
async def test_serial_process_packet_negotiating_non_baud_cmd(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    transport._negotiating = True
    transport._negotiation_future = asyncio.get_running_loop().create_future()

    # Frame with command that is NOT CMD_SET_BAUDRATE_RESP
    frame_bytes = build_frame(
        command_id=Command.CMD_GET_VERSION.value,
        payload=b"",
        sequence_id=1,
    )
    from mcubridge.transport.serial import cobsr

    encoded = cobsr.encode(frame_bytes)
    await transport._process_packet(encoded)
    assert not transport._negotiation_future.done()


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

    # Call with path=None to hit if path is not None: False branch
    await svc._file_dispatch_local_read("test", req, None)
    await svc._file_dispatch_local_write("test", req, None)
    await svc._file_dispatch_local_remove("test", req, None)


@pytest.mark.asyncio
async def test_runtime_handle_mcu_frame_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. serial is None
    svc.serial = None
    await svc.handle_mcu_frame(Command.CMD_GET_VERSION.value, 1, b"")

    # 2. unhandled command with known response_to_request mapping
    svc.serial = serial
    mock_state.mark_synchronized()
    await svc.handle_mcu_frame(Command.CMD_GET_VERSION.value, 1, pb.VersionResponse().SerializeToString())


@pytest.mark.asyncio
async def test_runtime_handle_request_route_none_and_inbound_props(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. Route is None (topic not matching prefix)
    await svc.handle_request(pb.CloudQueuedPublish(topic_name="unmatched/topic", payload=b""))

    # 2. Inbound object with properties containing ResponseTopic and CorrelationData
    class InboundProps:
        ResponseTopic = "cloud/resp"
        CorrelationData = b"corr456"

    class InboundObj:
        properties = InboundProps()
        topic = "bridge/unmatched"
        payload = b"testpayload"

    await svc.handle_request(InboundObj())


@pytest.mark.asyncio
async def test_runtime_handle_console_empty_payload(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    await svc._handle_console(pb.CloudQueuedPublish(topic_name="bridge/console", payload=b""))


@pytest.mark.asyncio
async def test_runtime_handle_datastore_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. PUT with oversized payload (> 512 bytes)
    route_put = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.PUT.value, "mykey")
    )
    await svc._handle_datastore(route_put, pb.CloudQueuedPublish(payload=b"x" * 600))

    # 2. GET cache miss without "request" suffix in remainder
    route_get = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.GET.value, "missingkey")
    )
    await svc._handle_datastore(route_get, pb.CloudQueuedPublish(payload=b""))

    # 3. Unknown identifier
    route_unknown = TopicRoute(raw="", prefix="bridge", topic=Topic.DATASTORE, segments=("unknown_act", "key"))
    await svc._handle_datastore(route_unknown, pb.CloudQueuedPublish(payload=b""))


@pytest.mark.asyncio
async def test_runtime_handle_mailbox_unknown_identifier(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    route = TopicRoute(raw="", prefix="bridge", topic=Topic.MAILBOX, segments=("unknown",))
    await svc._handle_mailbox(route, pb.CloudQueuedPublish(payload=b"test"))


@pytest.mark.asyncio
async def test_runtime_handle_file_unhandled_action_and_failed_writes(
    test_config: RuntimeConfig, mock_state: RuntimeState, tmp_path: Path
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. Unregistered (is_mcu, act) tuple
    route_unhandled = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=("custom_act", "mcu/file"))
    await svc._handle_file(route_unhandled, pb.CloudQueuedPublish(payload=b"data"))

    # 2. MCU write send fails
    serial.send = AsyncMock(return_value=False)
    await svc._handle_file_mcu_write("mcu/test.txt", pb.CloudQueuedPublish(payload=b"data"))

    # 3. MCU remove with serial=None
    svc.serial = None
    await svc._handle_file_mcu_remove("mcu/test.txt", pb.CloudQueuedPublish(payload=b""))
    svc.serial = serial

    # 4. Local write fails quota
    with patch.object(svc, "_write_with_quota", return_value=False):
        await svc._handle_file_local_write(tmp_path / "f.txt", "f.txt", pb.CloudQueuedPublish(payload=b"data"))

    # 5. Local read when path is not a file
    non_file = tmp_path / "not_a_file"
    await svc._handle_file_local_read(non_file, "not_a_file", pb.CloudQueuedPublish(topic_name="bridge/file/read"))

    # 6. Local read with response topic (skips re-publishing)
    real_file = tmp_path / "real.txt"
    real_file.write_text("hello")
    await svc._handle_file_local_read(
        real_file, "real.txt", pb.CloudQueuedPublish(topic_name="bridge/file/read/response")
    )


@pytest.mark.asyncio
async def test_runtime_handle_shell_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. Unregistered shell action
    route_unregistered = TopicRoute(raw="", prefix="bridge", topic=Topic.SHELL, segments=("unknown_act",))
    await svc._handle_shell(route_unregistered, pb.CloudQueuedPublish(payload=b""))

    # 2. run_async with protobuf payload bytes starting with \x0a
    proto_cmd = pb.ProcessRunAsync(command="echo hello").SerializeToString()
    route_run = TopicRoute(raw="", prefix="bridge", topic=Topic.SHELL, segments=(ShellAction.RUN_ASYNC.value,))
    await svc._handle_shell(route_run, pb.CloudQueuedPublish(payload=proto_cmd))

    # 3. kill with unknown pid
    await svc._handle_shell_kill(99999, pb.CloudQueuedPublish(payload=b""))


@pytest.mark.asyncio
async def test_runtime_handle_spi_and_pin_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. SPI transfer with empty payload
    route_spi_xfer = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=(SpiAction.TRANSFER.value,))
    await svc._handle_spi(route_spi_xfer, pb.CloudQueuedPublish(payload=b""))

    # 2. Pin handler with unknown action in segments[1]
    route_pin = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13", "unknown_action"))
    await svc._handle_pin(route_pin, pb.CloudQueuedPublish(payload=b"1"))


@pytest.mark.asyncio
async def test_runtime_handle_system_free_memory_non_bytes(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    serial.send = AsyncMock(return_value=False)
    svc = BridgeService(test_config, mock_state, serial)

    route = TopicRoute(raw="", prefix="bridge", topic=Topic.SYSTEM, segments=(SystemAction.FREE_MEMORY.value, "get"))
    await svc._handle_system(route, pb.CloudQueuedPublish(payload=b""))


@pytest.mark.asyncio
async def test_runtime_request_mcu_version_empty_topic(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    v_resp = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    serial.send = AsyncMock(return_value=v_resp)
    svc = BridgeService(test_config, mock_state, serial)

    with patch("mcubridge.services.runtime.get_topic_for_message", return_value=""):
        ok = await svc._request_mcu_version()
        assert ok is True


@pytest.mark.asyncio
async def test_runtime_monitor_process_none_ctx(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    # Monitor non-existent process
    await svc._monitor_process(99999)


@pytest.mark.asyncio
async def test_runtime_cloud_session_non_command_envelope(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
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

    with patch("mcubridge.services.runtime.Channel"):
        with patch("mcubridge.services.runtime.CloudBridgeStub", return_value=mock_stub):
            with patch.object(svc, "flush_cloud_spool", new_callable=AsyncMock):
                with patch.object(svc, "_send_cloud_event", new_callable=AsyncMock):
                    await svc.connect_cloud_session(None)


# ==========================================
# 5. Definitions & Client SPI Branches
# ==========================================


def test_definitions_build_bridge_args_empty() -> None:
    with patch.dict("os.environ", {}, clear=True):
        with patch("mcubridge_client.definitions.DEFAULT_SOCKET_PATH", ""):
            args = build_bridge_args(socket_path="", topic_prefix="")
            assert args == {}


@pytest.mark.asyncio
async def test_client_spi_transfer_branches() -> None:
    stub = AsyncMock()
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

    # Case 1: spool.popleft raises IndexError during limit trim
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = [2, 0]
    mock_spool.popleft.side_effect = IndexError("empty")
    mock_spool.append.return_value = None
    svc._cloud_spool = mock_spool
    msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"data")
    assert await svc._spool_cloud_message_locked(msg) is True

    # Case 2: spool.popleft raises OSError during limit trim
    mock_spool.length.side_effect = [2, 0]
    mock_spool.popleft.side_effect = OSError("IO failure")
    assert await svc._spool_cloud_message_locked(msg) is True

    # Case 3: spool.append raises OSError
    mock_spool.length.side_effect = [0, 0]
    mock_spool.append.side_effect = OSError("Disk full")
    assert await svc._spool_cloud_message_locked(msg) is False


@pytest.mark.asyncio
async def test_runtime_flush_cloud_spool_corrupt_and_errors(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    svc._cloud_stream = AsyncMock()

    # Case 1: Corrupt item with spool.popleft raising IndexError
    mock_spool = AsyncMock()
    mock_spool.length.side_effect = [1, 1, 0, 0, 0]
    mock_spool.peek.return_value = b"corrupt-data"
    mock_spool.popleft.side_effect = IndexError("empty")
    svc._cloud_spool = mock_spool
    await svc._flush_cloud_spool_locked()

    # Case 2: Corrupt item with spool.popleft raising OSError
    mock_spool.length.side_effect = [1, 1, 0, 0, 0]
    mock_spool.peek.return_value = b"corrupt-data"
    mock_spool.popleft.side_effect = OSError("IO Error")
    await svc._flush_cloud_spool_locked()

    # Case 3: Corrupt item with subsequent length() raising OSError
    mock_spool.length.side_effect = [1, OSError("DB error"), 0, 0]
    mock_spool.peek.return_value = b"corrupt-data"
    mock_spool.popleft.side_effect = None
    await svc._flush_cloud_spool_locked()

    # Case 4: Valid item with popleft raising OSError after publish
    valid_bytes = pb.CloudQueuedPublish(topic_name="br/t", payload=b"p").SerializeToString()
    mock_spool.length.side_effect = [1, 0, 0, 0]
    mock_spool.peek.return_value = valid_bytes
    mock_spool.popleft.side_effect = OSError("DB lock error")
    with patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
        await svc._flush_cloud_spool_locked()

    # Case 5: Valid item with subsequent length() raising OSError after publish
    mock_spool.length.side_effect = [1, OSError("DB length error"), 0, 0]
    mock_spool.peek.return_value = valid_bytes
    mock_spool.popleft.side_effect = None
    with patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
        await svc._flush_cloud_spool_locked()

    # Case 6: Vacuum raising OSError
    mock_spool.length.side_effect = [0, 0, 0]
    mock_spool.vacuum.side_effect = OSError("Vacuum disk fail")
    await svc._flush_cloud_spool_locked()


@pytest.mark.asyncio
async def test_runtime_handle_mcu_status_unusual_payloads(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. Non-bytes, non-protobuf payload
    await svc._handle_mcu_status(Status.TIMEOUT, 1, cast(Any, 12345))

    # 2. Non-utf8 binary bytes falling back to hex representation
    non_utf8 = b"\xff\xfe\xfd\x80"
    await svc._handle_mcu_status(Status.CRC_MISMATCH, 2, non_utf8)


@pytest.mark.asyncio
async def test_runtime_handle_datastore_empty_key_and_request_miss(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # Empty key
    route_empty = TopicRoute(raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.PUT.value,))
    await svc._handle_datastore(route_empty, pb.CloudQueuedPublish(payload=b"val"))

    # GET with 'request' in remainder and cache miss
    route_req = TopicRoute(
        raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.GET.value, "key1", "request")
    )
    with patch.object(svc, "_publish_datastore_value", new_callable=AsyncMock) as mock_pub:
        await svc._handle_datastore(route_req, pb.CloudQueuedPublish(payload=b""))
        mock_pub.assert_called_once_with(
            "key1/request",
            b"",
            reply_context=pytest.approx(mock_pub.call_args[1]["reply_context"]),
            error="datastore-miss",
        )


@pytest.mark.asyncio
async def test_runtime_handle_mailbox_edge_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # serial=None
    svc.serial = None
    route = TopicRoute(raw="", prefix="bridge", topic=Topic.MAILBOX, segments=("write",))
    await svc._handle_mailbox(route, pb.CloudQueuedPublish(payload=b"hi"))

    # Read with empty incoming queue
    svc.serial = serial
    await mock_state.mailbox_incoming_queue.clear()
    route_read = TopicRoute(raw="", prefix="bridge", topic=Topic.MAILBOX, segments=("read",))
    await svc._handle_mailbox(route_read, pb.CloudQueuedPublish(payload=b""))


@pytest.mark.asyncio
async def test_runtime_handle_file_and_shell_edge_branches(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # File with serial=None
    svc.serial = None
    route_file = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=("read", "test.txt"))
    await svc._handle_file(route_file, pb.CloudQueuedPublish(payload=b""))

    # File with empty remainder
    svc.serial = serial
    route_no_rem = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=())
    await svc._handle_file(route_no_rem, pb.CloudQueuedPublish(payload=b""))

    # File safe path returning None for unsafe target
    route_unsafe = TopicRoute(raw="", prefix="bridge", topic=Topic.FILE, segments=("read", "../../../etc/shadow"))
    with patch.object(svc, "_get_safe_path", return_value=None):
        await svc._handle_file(route_unsafe, pb.CloudQueuedPublish(payload=b""))

    # Shell with empty segments
    route_shell_empty = TopicRoute(raw="", prefix="bridge", topic=Topic.SHELL, segments=())
    await svc._handle_shell(route_shell_empty, pb.CloudQueuedPublish(payload=b""))

    # Shell run_async with inbound properties ContentType
    class Props:
        ContentType = "application/x-protobuf"

    class InboundWithProps:
        payload = pb.ProcessRunAsync(command="echo prop").SerializeToString()
        properties = Props()

    with patch.object(svc, "_run_process", new_callable=AsyncMock, return_value=123):
        await svc._handle_shell_run_async(0, cast(Any, InboundWithProps()))

    # Shell kill raising ProcessLookupError
    mock_ctx = ProcessContext(AsyncMock())
    mock_state.running_processes[777] = mock_ctx
    with patch.object(svc, "_terminate_process", side_effect=ProcessLookupError("No such process")):
        await svc._handle_shell_kill(777, pb.CloudQueuedPublish(payload=b""))


@pytest.mark.asyncio
async def test_runtime_handle_spi_and_pin_edge_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # SPI with serial=None
    svc.serial = None
    route_spi = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("begin",))
    await svc._handle_spi(route_spi, pb.CloudQueuedPublish(payload=b""))

    # SPI config with invalid protobuf
    svc.serial = serial
    route_spi_cfg = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("config",))
    await svc._handle_spi(route_spi_cfg, pb.CloudQueuedPublish(payload=b"not-proto"))

    # SPI transfer with non-bytes response
    serial.send.return_value = False
    route_spi_xfer = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("transfer",))
    await svc._handle_spi(route_spi_xfer, pb.CloudQueuedPublish(payload=b"data"))

    # Pin with serial=None
    svc.serial = None
    route_pin = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13", "write"))
    await svc._handle_pin(route_pin, pb.CloudQueuedPublish(payload=b"1"))

    # Pin with invalid segments (< 2 segments or non-digit pin)
    svc.serial = serial
    route_pin_invalid = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("invalid_pin", "write"))
    await svc._handle_pin(route_pin_invalid, pb.CloudQueuedPublish(payload=b"1"))

    route_pin_short = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13",))
    await svc._handle_pin(route_pin_short, pb.CloudQueuedPublish(payload=b"1"))


@pytest.mark.asyncio
async def test_runtime_request_mcu_version_failures(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. serial is None
    svc.serial = None
    assert await svc._request_mcu_version() is False

    # 2. serial.send returns False
    svc.serial = serial
    serial.send.return_value = False
    assert await svc._request_mcu_version() is False
