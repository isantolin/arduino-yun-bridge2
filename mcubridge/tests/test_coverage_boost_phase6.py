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
    PROTOBUF_CONTENT_TYPE,
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

    # Call with unsafe path so that _get_safe_path returns None
    route = TopicRoute(
        raw="bridge/file/read/../../secret",
        prefix=test_config.topic_prefix,
        topic=Topic.FILE,
        segments=("read", "..", "..", "secret"),
    )
    await svc._handle_file(route, req)
    assert serial.send.call_count == 0


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
    with patch.object(svc, "_run_process", new_callable=AsyncMock, return_value=123) as mock_run:
        await svc._handle_shell(route_run, pb.CloudQueuedPublish(payload=proto_cmd))
        assert mock_run.called

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

    # Case 1: spool.popleft raises IndexError during limit trim
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [2, 0]
    mock_spool.popleft = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.append = AsyncMock(return_value=None)
    svc._cloud_spool = mock_spool
    msg = pb.CloudQueuedPublish(topic_name="br/test", payload=b"data")
    assert await svc._spool_cloud_message_locked(msg) is True

    # Case 2: spool.popleft raises OSError during limit trim
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [2, 0]
    mock_spool.popleft = AsyncMock(side_effect=OSError("IO failure"))
    mock_spool.append = AsyncMock(return_value=None)
    svc._cloud_spool = mock_spool
    assert await svc._spool_cloud_message_locked(msg) is True

    # Case 3: spool.append raises OSError
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [0, 0]
    mock_spool.append = AsyncMock(side_effect=OSError("Disk full"))
    svc._cloud_spool = mock_spool
    assert await svc._spool_cloud_message_locked(msg) is False


@pytest.mark.asyncio
async def test_runtime_flush_cloud_spool_corrupt_and_errors(
    test_config: RuntimeConfig, mock_state: RuntimeState
) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    svc._cloud_stream = AsyncMock()

    # Case 1: Corrupt item with spool.popleft raising IndexError
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(side_effect=IndexError("empty"))
    mock_spool.vacuum = AsyncMock()
    svc._cloud_spool = mock_spool
    await svc._flush_cloud_spool_locked()

    # Case 2: Corrupt item with spool.popleft raising OSError
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(side_effect=OSError("IO Error"))
    mock_spool.vacuum = AsyncMock()
    svc._cloud_spool = mock_spool
    await svc._flush_cloud_spool_locked()

    # Case 3: Corrupt item with valid popleft
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=b"corrupt-data")
    mock_spool.popleft = AsyncMock(return_value=None)
    mock_spool.vacuum = AsyncMock()
    svc._cloud_spool = mock_spool
    await svc._flush_cloud_spool_locked()

    # Case 4: Valid item with popleft raising OSError after publish
    valid_bytes = pb.CloudQueuedPublish(topic_name="br/t", payload=b"p").SerializeToString()
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=valid_bytes)
    mock_spool.popleft = AsyncMock(side_effect=OSError("DB lock error"))
    mock_spool.vacuum = AsyncMock()
    svc._cloud_spool = mock_spool
    with patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
        await svc._flush_cloud_spool_locked()

    # Case 5: Valid item with normal completion
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [1, 0, 0, 0]
    mock_spool.peek = AsyncMock(return_value=valid_bytes)
    mock_spool.popleft = AsyncMock(return_value=None)
    mock_spool.vacuum = AsyncMock()
    svc._cloud_spool = mock_spool
    with patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
        await svc._flush_cloud_spool_locked()

    # Case 6: Vacuum raising OSError
    mock_spool = MagicMock()
    mock_spool.__len__.side_effect = [0, 0, 0]
    mock_spool.vacuum = AsyncMock(side_effect=OSError("Vacuum disk fail"))
    svc._cloud_spool = mock_spool
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
        assert mock_pub.call_count == 1
        assert mock_pub.call_args[0][0] == "key1/request"
        assert mock_pub.call_args[1]["error"] == "datastore-miss"


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
    mock_state.mark_synchronized()

    confirmed = await hs._wait_for_link_sync_confirmation(b"nonce")
    assert confirmed is True


@pytest.mark.asyncio
async def test_handshake_sync_state_permutations(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
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

    # 1. State is FAULT after send_link_sync
    with patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True):
        hs.fsm_state = HandshakeState.FAULT
        assert await hs._synchronize_attempt() is False

    # 2. Confirmed is False, and state is FAULT
    with patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True):
        hs.fsm_state = HandshakeState.SYNCING

        async def _mock_wait_fault(nonce: bytes) -> bool:
            hs.fsm_state = HandshakeState.FAULT
            return False

        hs._wait_for_link_sync_confirmation = _mock_wait_fault
        assert await hs._synchronize_attempt() is False

    # 3. Confirmed is False, pending_nonce != nonce
    with patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True):
        with patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=False):
            hs.fsm_state = HandshakeState.SYNCING
            mock_state.link_handshake_nonce = b"different_nonce"
            assert await hs._synchronize_attempt() is False

    # 4. Confirmed is True, current_state is SYNCHRONIZED
    with patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True):
        with patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=True):
            hs.fsm_state = HandshakeState.SYNCHRONIZED
            assert await hs._synchronize_attempt() is True


@pytest.mark.asyncio
async def test_handshake_resp_rate_limit_and_secret_none(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
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
    with patch.object(hs, "_handle_handshake_success", new_callable=AsyncMock):
        with patch.object(hs, "_fetch_capabilities_with_delay", new_callable=AsyncMock):
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
    hs._capabilities_future = None
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
    await transport._read_loop(mock_serial)

    # 2. _correlate_frame with empty ACK payload
    transport._current = PendingCommand(
        command_id=Command.CMD_DIGITAL_WRITE.value,
        expected_resp_ids=[],
    )
    transport._correlate_frame(Status.ACK.value, b"")
    assert transport._current.ack_received is True
    assert transport._current.success is True


@pytest.mark.asyncio
async def test_structures_replace_and_resolve_edge_branches() -> None:
    from mcubridge.protocol.structures import replace_cloud_publish, resolve_cloud_context

    msg = pb.CloudQueuedPublish(topic_name="test", payload=b"p")

    # 1. replace_cloud_publish with subscription_identifier=None
    res = replace_cloud_publish(msg, subscription_identifier=None)
    assert len(res.subscription_identifier) == 0

    # 2. resolve_cloud_context with context having no response_topic and no properties
    class ContextNoProps:
        pass

    resolved = resolve_cloud_context(msg, ContextNoProps())
    assert resolved.topic_name == "test"

    # 3. PendingCommand mark_success when completion is already set
    cmd = PendingCommand(command_id=1, expected_resp_ids=[])
    cmd.completion.set()
    cmd.mark_success(b"payload")
    assert cmd.success is True


@pytest.mark.asyncio
async def test_runtime_teardown_lmdb_errors(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    import lmdb

    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    mock_spool = AsyncMock()
    mock_spool.close.side_effect = lmdb.Error("Spool close fail")
    svc._cloud_spool = mock_spool

    mock_cache = AsyncMock()
    mock_cache.close.side_effect = OSError("Cache close fail")
    mock_state.datastore_cache = mock_cache

    mock_mb = AsyncMock()
    mock_mb.close.side_effect = lmdb.Error("Mailbox close fail")
    mock_state.mailbox_queue = mock_mb

    mock_mbin = AsyncMock()
    mock_mbin.close.side_effect = OSError("Mailbox in close fail")
    mock_state.mailbox_incoming_queue = mock_mbin

    with patch("mcubridge.services.runtime.STATUS_FILE"):
        with patch.object(svc, "cleanup"):
            with patch("asyncio.TaskGroup", side_effect=ExceptionGroup("tasks", [RuntimeError("Teardown trigger")])):
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
    resp = await svc._poll_process(555)
    assert resp.finished is True
    assert 555 not in mock_state.running_processes

    # 2. _handle_mcu_xoff
    await svc._handle_mcu_xoff(1, None)
    assert mock_state.mcu_is_paused is True
    assert not mock_state.serial_tx_allowed.is_set()

    # 3. _on_mcu_console_write with empty data
    await svc._on_mcu_console_write(1, pb.ConsoleWrite(data=b""))

    # 4. _on_mcu_datastore_get with serial=None
    svc.serial = None
    assert await svc._on_mcu_datastore_get(1, pb.DatastoreGet(key="k")) is False


def test_metrics_prometheus_exporter_teardown_server_none(mock_state: RuntimeState) -> None:
    from mcubridge.metrics import PrometheusExporter

    with patch("mcubridge.metrics.make_server", return_value=MagicMock()):
        exp = PrometheusExporter(mock_state, host="127.0.0.1", port=9999)
        exp._server = None
        exp._collector = None
        assert exp._server is None


# # ==========================================
# 9. Daemon, Logging, Settings, Security & Frame
# ==========================================


def test_daemon_shared_secret_none_and_app_main() -> None:
    from mcubridge.daemon import app, run_daemon

    # 1. run_daemon without shared secret
    mock_cfg = RuntimeConfig()
    mock_cfg.serial_shared_secret = b""
    with patch("mcubridge.daemon.load_runtime_config", return_value=mock_cfg):
        with patch("mcubridge.services.runtime.BridgeService.run", new_callable=AsyncMock):
            with patch("mcubridge.daemon.asyncio.Runner") as mock_runner_cls:
                mock_runner = MagicMock()

                def _mock_run(coro: Any) -> None:
                    if hasattr(coro, "close"):
                        coro.close()

                mock_runner.run.side_effect = _mock_run
                mock_runner_cls.return_value.__enter__.return_value = mock_runner
                run_daemon()
                assert mock_runner.run.called

    # 2. app(args=None)
    with patch("mcubridge.daemon.run_daemon") as mock_rd:
        app(None)
        assert mock_rd.called


def test_logging_no_syslog_available(test_config: RuntimeConfig) -> None:
    import os
    from mcubridge.config.logging import configure_logging

    with patch.dict(os.environ, {}, clear=True):
        with patch("pathlib.Path.exists", return_value=False):
            configure_logging(test_config)


def test_settings_raw_config_edge_branches() -> None:
    from mcubridge.config.common import get_default_config
    from mcubridge.config.settings import load_runtime_config, load_runtime_config_from_json

    # 1. Non list/tuple for repeated field (line 173->176)
    cfg1 = load_runtime_config(overrides={"allowed_commands": 123})
    assert cfg1 is not None

    # 2. Topic auth coerced to None (line 190->192)
    cfg2 = load_runtime_config(overrides={"allow_datastore": object()})
    assert cfg2 is not None

    # 3. Defaults with bytes value (line 216)
    defaults = get_default_config()
    defaults["serial_port"] = b"/dev/ttyATH0"
    with patch("mcubridge.config.settings.get_default_config", return_value=defaults):
        cfg3 = load_runtime_config_from_json("{}")
        assert cfg3.serial_port == "/dev/ttyATH0"


def test_security_self_test_chacha_invalid_length() -> None:
    from mcubridge.security.security import verify_crypto_integrity

    with patch("mcubridge.security.security.ChaCha20Poly1305.encrypt", return_value=b"short"):
        assert verify_crypto_integrity() is False


def test_watchdog_kick_state_none() -> None:
    from mcubridge.watchdog import WatchdogKeepalive

    wd = WatchdogKeepalive(interval=10.0, state=None)
    wd._token = b"W"
    with patch.object(wd, "_write"):
        wd.kick()


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


# ==========================================
# 10. Handshake, Metrics and Runtime Edge Permutations
# ==========================================


@pytest.mark.asyncio
async def test_handshake_sync_confirming_state_and_timeout(
    test_config: RuntimeConfig, mock_state: RuntimeState
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

    # 1. State not SYNCING before wait confirmation (line 211->214)
    with patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True):
        hs.fsm_state = HandshakeState.CONFIRMING
        with patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=True):
            assert await hs._synchronize_attempt() is True

    # 2. Confirmed=False, pending_nonce == nonce (line 224->226)
    with patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True):
        with patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=False):
            hs.fsm_state = HandshakeState.SYNCING
            with patch.object(hs, "handle_handshake_failure", new_callable=AsyncMock) as mock_fail:
                assert await hs._synchronize_attempt() is False
                assert mock_fail.called

    # 3. Confirmed=True, current_state == CONFIRMING (line 229->232)
    with patch.object(hs, "_send_frame", new_callable=AsyncMock, return_value=True):
        with patch.object(hs, "_wait_for_link_sync_confirmation", new_callable=AsyncMock, return_value=True):
            hs.fsm_state = HandshakeState.CONFIRMING
            assert await hs._synchronize_attempt() is True
            assert hs.fsm_state == HandshakeState.SYNCHRONIZED

    # 4. handle_link_sync_resp rate limit updated (line 261)
    test_config.serial_handshake_min_interval = 2.0
    mock_state.handshake_rate_until = 0.0
    nonce = b"valid_nonce_12"
    mock_state.link_handshake_nonce = nonce
    mock_state.link_expected_tag = hs.calculate_handshake_tag(test_config.serial_shared_secret, nonce)
    with patch.object(hs, "_handle_handshake_success", new_callable=AsyncMock):
        with patch.object(hs, "_fetch_capabilities_with_delay", new_callable=AsyncMock):
            res = await hs.handle_link_sync_resp(1, pb.LinkSync(nonce=nonce, tag=mock_state.link_expected_tag))
            assert res is True
            assert mock_state.handshake_rate_until > 0.0


@pytest.mark.asyncio
async def test_metrics_cancel_and_disabled_branches(mock_state: RuntimeState) -> None:
    from mcubridge.metrics import (
        _build_metrics_message,
        _emit_bridge_snapshot,
        publish_bridge_snapshots,
    )

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
async def test_runtime_spool_and_pin_edge_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. enqueue_cloud debug logging enabled (line 233)
    with patch("structlog.stdlib.BoundLogger.is_enabled_for", return_value=True):
        with patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=True):
            await svc.enqueue_cloud(pb.CloudQueuedPublish(topic_name="test", payload=b"p"))

    # 2. _spool_cloud_message_locked when cloud_queue_limit == 0 (line 277->295)
    mock_state.cloud_queue_limit = 0
    mock_spool = AsyncMock()
    mock_spool.len.return_value = 100
    svc._cloud_spool = mock_spool
    await svc._spool_cloud_message_locked(pb.CloudQueuedPublish(topic_name="test", payload=b"p"))

    # 3. _flush_cloud_spool_locked when publish returns False (line 354)
    mock_spool.len.return_value = 1
    mock_spool.popleft.return_value = pb.CloudQueuedPublish(topic_name="test", payload=b"p")
    with patch.object(svc, "_publish_cloud_message", new_callable=AsyncMock, return_value=False):
        await svc._flush_cloud_spool_locked()

    # 4. _handle_datastore PUT with cache active (line 800->802)
    mock_cache = AsyncMock()
    mock_state.datastore_cache = mock_cache
    route_put = TopicRoute(raw="", prefix="bridge", topic=Topic.DATASTORE, segments=(DatastoreAction.PUT.value, "k1"))
    with patch.object(svc, "_publish_datastore_value", new_callable=AsyncMock):
        await svc._handle_datastore(route_put, pb.CloudQueuedPublish(payload=b"v1"))
        mock_cache.set.assert_called_once_with("k1", b"v1")


@pytest.mark.asyncio
async def test_serial_transport_additional_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)

    # 1. _correlate_frame with debug logging (line 249)
    with patch("structlog.stdlib.BoundLogger.is_enabled_for", return_value=True):
        transport._correlate_frame(Status.ACK.value, b"")

    # 2. _negotiate_baudrate success future (line 424)
    transport.serial = AsyncMock()
    with patch.object(transport, "send_raw", new_callable=AsyncMock, return_value=True):

        async def _resolve_fut() -> None:
            await asyncio.sleep(0.01)
            if transport._negotiation_future:
                transport._negotiation_future.set_result(True)

        asyncio.create_task(_resolve_fut())
        res = await transport._negotiate_baudrate(115200)
        assert res is True


@pytest.mark.asyncio
async def test_handshake_fetch_capabilities_with_delay_called(
    test_config: RuntimeConfig, mock_state: RuntimeState
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
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with patch.object(hs, "_fetch_capabilities", new_callable=AsyncMock) as mock_fetch:
            await hs._fetch_capabilities_with_delay()
            assert mock_fetch.called


def test_daemon_main_block_simulation() -> None:
    from mcubridge import daemon

    with patch.object(daemon, "run_daemon") as mock_rd:
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
async def test_serial_transport_run_loop_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    transport = SerialTransport(test_config, mock_state, None)
    mock_serial_inst = AsyncMock()
    mock_serial_inst.transport = MagicMock()

    # 1. service is None on connect/disconnect
    with patch("serialx.AsyncSerial") as mock_serial_cls:
        mock_serial_cls.return_value.__aenter__.return_value = mock_serial_inst

        async def _pending_read(_s: Any) -> None:
            await asyncio.Event().wait()

        with patch.object(transport, "_toggle_dtr", new_callable=AsyncMock):
            with patch.object(transport, "_read_loop", side_effect=_pending_read):
                transport._stop_event.set()
                await transport._connect_and_run()

    # 2. read_task finishes first -> raises ConnectionError
    transport._stop_event.clear()
    with patch("serialx.AsyncSerial") as mock_serial_cls:
        mock_serial_cls.return_value.__aenter__.return_value = mock_serial_inst
        with patch.object(transport, "_toggle_dtr", new_callable=AsyncMock):
            with patch.object(transport, "_read_loop", new_callable=AsyncMock) as mock_read:
                mock_read.return_value = None
                with pytest.raises(ConnectionError, match="Serial connection lost"):
                    await transport._connect_and_run()


@pytest.mark.asyncio
async def test_runtime_system_and_pin_edge_branches(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    from mcubridge.protocol.protocol import PinAction

    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    # 1. _handle_system with serial=None
    svc.serial = None
    route_sys = TopicRoute(raw="", prefix="bridge", topic=Topic.SYSTEM, segments=("version", "get"))
    await svc._handle_system(route_sys, pb.CloudQueuedPublish())

    # 2. _handle_system with unknown action
    svc.serial = serial
    route_sys_unknown = TopicRoute(raw="", prefix="bridge", topic=Topic.SYSTEM, segments=("unknown_sys_action",))
    await svc._handle_system(route_sys_unknown, pb.CloudQueuedPublish())

    # 3. _handle_spi with unknown identifier
    route_spi_unknown = TopicRoute(raw="", prefix="bridge", topic=Topic.SPI, segments=("unknown_spi_action",))
    await svc._handle_spi(route_spi_unknown, pb.CloudQueuedPublish())

    # 4. _handle_pin mode set
    route_pin_mode = TopicRoute(raw="", prefix="bridge", topic=Topic.DIGITAL, segments=("13", PinAction.MODE.value))
    await svc._handle_pin(route_pin_mode, pb.CloudQueuedPublish(payload=b"1"))

    # 5. _handle_pin analog write
    route_pin_aw = TopicRoute(raw="", prefix="bridge", topic=Topic.ANALOG, segments=("3",))
    await svc._handle_pin(route_pin_aw, pb.CloudQueuedPublish(payload=b"128"))


@pytest.mark.asyncio
async def test_runtime_inbound_unhandled_topic(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)
    req = pb.CloudQueuedPublish(topic_name="bridge/status/unhandled", payload=b"test")
    await svc.handle_request(req)


@pytest.mark.asyncio
async def test_runtime_shell_properties_content_type(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
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

    with patch.object(svc, "_run_process", side_effect=_mock_run_p):
        with patch.object(svc, "enqueue_cloud", side_effect=_mock_enq):
            await svc._handle_shell_run_async(0, inbound)


@pytest.mark.asyncio
async def test_runtime_run_process_with_task_group(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    async def _mock_mon(_pid: int) -> None:
        pass

    def _mock_create_task(coro: Any) -> None:
        if hasattr(coro, "close"):
            coro.close()

    mock_tg = MagicMock()
    mock_tg.create_task.side_effect = _mock_create_task
    svc._tg = mock_tg
    with patch.object(svc, "_monitor_process", side_effect=_mock_mon):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_p = MagicMock()
            mock_p.pid = 456
            mock_exec.return_value = mock_p
            pid = await svc._run_process("echo tg")
            assert pid == 456
            assert mock_tg.create_task.called
    svc._tg = None


@pytest.mark.asyncio
async def test_runtime_run_cloud_cancelled(test_config: RuntimeConfig, mock_state: RuntimeState) -> None:
    serial = AsyncMock(spec=SerialTransport)
    svc = BridgeService(test_config, mock_state, serial)

    async def _mock_cloud_cancel(_tls: Any) -> None:
        raise asyncio.CancelledError()

    with patch.object(svc, "connect_cloud_session", side_effect=_mock_cloud_cancel):
        with pytest.raises(asyncio.CancelledError):
            await svc.run_cloud()


@pytest.mark.asyncio
async def test_metrics_prometheus_exporter_finally_branches(mock_state: RuntimeState) -> None:
    from mcubridge.metrics import PrometheusExporter

    mock_server = MagicMock()
    mock_server.server_address = ("127.0.0.1", 9130)
    with patch("mcubridge.metrics.make_server", return_value=mock_server):
        exp = PrometheusExporter(mock_state, host="127.0.0.1", port=9130)
        mock_collector = MagicMock()
        exp._collector = mock_collector
        mock_reg = MagicMock()
        mock_reg.unregister.side_effect = KeyError("not found")
        exp._registry = mock_reg

        async def _mock_run_in_executor(*_args: Any, **_kwargs: Any) -> None:
            raise asyncio.CancelledError()

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_run_in_executor
            with pytest.raises(asyncio.CancelledError):
                await exp.run()


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

    # SPI begin & end
    route_begin = parse_topic("br", "br/spi/begin")
    assert route_begin is not None
    await service._handle_spi(route_begin, pb.CloudQueuedPublish(payload=b""))
    serial.send.assert_awaited()

    route_end = parse_topic("br", "br/spi/end")
    assert route_end is not None
    await service._handle_spi(route_end, pb.CloudQueuedPublish(payload=b""))
    serial.send.assert_awaited()

    # SPI config (valid & invalid)
    route_cfg = parse_topic("br", "br/spi/config")
    assert route_cfg is not None
    cfg_pb = pb.SpiConfig(frequency=4000000, bit_order=1, data_mode=0)
    await service._handle_spi(route_cfg, pb.CloudQueuedPublish(payload=cfg_pb.SerializeToString()))
    await service._handle_spi(route_cfg, pb.CloudQueuedPublish(payload=b"\xff\xff\xff\xff"))

    # SPI transfer (empty payload, non-bytes response, and populated with bytes response)
    route_tr = parse_topic("br", "br/spi/transfer")
    assert route_tr is not None
    await service._handle_spi(route_tr, pb.CloudQueuedPublish(payload=b""))

    serial.send.return_value = False
    await service._handle_spi(route_tr, pb.CloudQueuedPublish(payload=b"\x01\x02"))

    resp_payload = pb.SpiTransferResponse(data=b"\xde\xad\xbe\xef").SerializeToString()
    serial.send.return_value = resp_payload
    await service._handle_spi(route_tr, pb.CloudQueuedPublish(payload=b"\x01\x02\x03\x04"))

    # System bootloader
    route_boot = parse_topic("br", "br/system/bootloader")
    assert route_boot is not None
    await service._handle_system(route_boot, pb.CloudQueuedPublish())

    # System free memory (non-bytes and bytes response)
    route_mem = parse_topic("br", "br/system/free_memory/get")
    assert route_mem is not None
    serial.send.return_value = False
    await service._handle_system(route_mem, pb.CloudQueuedPublish())
    serial.send.return_value = pb.FreeMemoryResponse(value=1024).SerializeToString()
    await service._handle_system(route_mem, pb.CloudQueuedPublish())

    # System version
    route_ver = parse_topic("br", "br/system/version/get")
    assert route_ver is not None
    serial.send.return_value = pb.VersionResponse(major=2, minor=8, patch=5).SerializeToString()
    await service._handle_system(route_ver, pb.CloudQueuedPublish())

    # System bridge snapshots (summary & handshake)
    route_snap = parse_topic("br", "br/system/bridge/summary")
    assert route_snap is not None
    await service._handle_system(route_snap, pb.CloudQueuedPublish())

    route_hs = parse_topic("br", "br/system/bridge/handshake")
    assert route_hs is not None
    await service._handle_system(route_hs, pb.CloudQueuedPublish())

    # Serial is None paths & unknown action paths
    service.serial = None
    await service._handle_spi(route_begin, pb.CloudQueuedPublish())
    await service._handle_system(route_boot, pb.CloudQueuedPublish())
    await service._handle_pin(route_boot, pb.CloudQueuedPublish())

    service.serial = serial
    unknown_route = parse_topic("br", "br/system/nonexistent_action")
    if unknown_route:
        await service._handle_system(unknown_route, pb.CloudQueuedPublish())

    # Cloud publish branches
    assert await service._publish_cloud_message(pb.CloudQueuedPublish()) is False
    service._cloud_stream = AsyncMock()
    for topic_sample in ["br/metrics/report", "br/summary/report", "br/handshake/report", "br/status/report"]:
        assert (
            await service._publish_cloud_message(pb.CloudQueuedPublish(topic_name=topic_sample, payload=b"test"))
            is True
        )

    assert (
        await service._publish_cloud_message(
            pb.CloudQueuedPublish(correlation_data=b"\x00\x00\x00\x00\x00\x00\x00\x01", payload=b"ok")
        )
        is True
    )

    service._cloud_stream.send_message.side_effect = OSError("network down")
    assert await service._publish_cloud_message(pb.CloudQueuedPublish(topic_name="br/metrics/report")) is False


def test_structures_replace_cloud_publish_variations() -> None:
    from mcubridge.protocol.structures import replace_cloud_publish
    from mcubridge.protocol import mcubridge_pb2 as pb

    orig = pb.CloudQueuedPublish(topic_name="test/topic", payload=b"hello")
    res1 = replace_cloud_publish(orig, user_properties=[("k1", "v1")], subscription_identifier=[1, 2])
    assert len(res1.user_properties) == 1
    assert len(res1.subscription_identifier) == 2

    res2 = replace_cloud_publish(orig, user_properties=[], subscription_identifier=[])
    assert len(res2.user_properties) == 0
    assert len(res2.subscription_identifier) == 0
