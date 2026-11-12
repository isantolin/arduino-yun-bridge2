"""Unit tests for LocalBridgeService typed gRPC methods. [SIL-2]"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
from dirty_equals import IsPositiveInt
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pytest_mock import MockerFixture
from grpclib.server import Stream

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import BY_NAME, RPC_SPECS
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import ProcessContext, create_runtime_state
from tools.testing.mock_stream import MockGrpcStream


class MockLocalBridgeClient:
    """Test harness simulating client-side gRPC invocation via BridgeService.execute_rpc. [SIL-2]"""

    def __init__(self, service: BridgeService) -> None:
        self.service = service

    async def _invoke_rpc(self, method_name: str, stream: MockGrpcStream[Any, Any]) -> None:
        req = await stream.recv_message()
        payload = req.SerializeToString() if req is not None else b""
        res_bytes = await self.service.execute_rpc(method_name, payload)
        spec = BY_NAME[method_name]
        resp_cls = spec.response_type or cloud_gateway_pb2.GenericResponse
        resp = resp_cls()
        if res_bytes:
            resp.ParseFromString(res_bytes)
        await stream.send_message(resp)

    def __getattr__(self, name: str) -> Any:
        if name in ("Publish", "SubscribeConsole"):
            return getattr(self.service, name)
        if name in BY_NAME:
            return lambda stream: self._invoke_rpc(name, stream)
        if hasattr(self.service, name):
            return getattr(self.service, name)
        raise AttributeError(f"MockLocalBridgeClient has no attribute '{name}'")


def _make_config(tmp_path: Path) -> RuntimeConfig:
    cfg = RuntimeConfig()
    cfg.file_system_root = str(tmp_path)
    cfg.allowed_policy.entries.extend(["echo", "sleep", "test_cmd"])
    cfg.allow_non_tmp_paths = True
    return cfg


def _make_service(config: RuntimeConfig) -> tuple[BridgeService, MockLocalBridgeClient, AsyncMock]:
    state = create_runtime_state(config)
    mock_serial = AsyncMock()
    mock_serial.send = AsyncMock(return_value=True)
    service = BridgeService(config=config, state=state, serial=mock_serial)
    return service, MockLocalBridgeClient(service), mock_serial


def _make_mock_stream(req_msg: object) -> MockGrpcStream[Any, Any]:
    return MockGrpcStream(request=req_msg)


def test_rpc_dispatch_table_matches_unary_service_descriptor() -> None:
    service_desc = cloud_gateway_pb2.DESCRIPTOR.services_by_name["LocalBridge"]
    methods = service_desc.methods
    unary_methods = {
        method.name: method for method in methods if not method.client_streaming and not method.server_streaming
    }

    local_rpc_map = {op.rpc_name or op.name: op for op in RPC_SPECS}
    assert set(local_rpc_map) == set(unary_methods)
    assert "SubscribeConsole" not in local_rpc_map
    for name, method in unary_methods.items():
        op = local_rpc_map[name]
        assert op.request_type is not None
        assert op.request_type.DESCRIPTOR.full_name == method.input_type.full_name
        assert op.handler_name is not None
        assert (op.rpc_name or op.name) in BY_NAME


async def test_local_bridge_pin_operations(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. SetPinMode
    stream = _make_mock_stream(mcu_wire_pb2.PinMode(pin=13, mode=config_schema_pb2.PIN_OUTPUT))
    await local_svc.SetPinMode(stream)
    mock_serial.send.assert_called_with(
        protocol.get_operation("SetPinMode"), mcu_wire_pb2.PinMode(pin=13, mode=config_schema_pb2.PIN_OUTPUT)
    )
    sent = stream.sent_message
    assert sent.status == "ok"

    # None request
    stream_none = _make_mock_stream(None)
    await local_svc.SetPinMode(stream_none)

    # 2. DigitalWrite
    stream = _make_mock_stream(mcu_wire_pb2.DigitalWrite(pin=13, value=1))
    await local_svc.DigitalWrite(stream)
    mock_serial.send.assert_called_with(
        protocol.get_operation("DigitalWrite"), mcu_wire_pb2.DigitalWrite(pin=13, value=1)
    )
    sent = stream.sent_message
    assert sent.status == "ok"

    # 3. DigitalRead (direct object)
    mock_serial.send.return_value = mcu_wire_pb2.DigitalReadResponse(value=1)
    stream = _make_mock_stream(mcu_wire_pb2.PinRead(pin=13))
    await local_svc.DigitalRead(stream)
    sent = stream.sent_message
    assert sent.value == 1

    # DigitalRead (no serial)
    service.serial = None
    stream = _make_mock_stream(mcu_wire_pb2.PinRead(pin=13))
    await local_svc.DigitalRead(stream)
    sent = stream.sent_message
    assert sent.value == 0

    # 4. AnalogWrite
    service.serial = mock_serial
    mock_serial.send.return_value = True
    stream = _make_mock_stream(mcu_wire_pb2.AnalogWrite(pin=9, value=128))
    await local_svc.AnalogWrite(stream)
    mock_serial.send.assert_called_with(
        protocol.get_operation("AnalogWrite"), mcu_wire_pb2.AnalogWrite(pin=9, value=128)
    )
    sent = stream.sent_message
    assert sent.status == "ok"

    # 5. AnalogRead
    mock_serial.send.return_value = mcu_wire_pb2.AnalogReadResponse(value=512)
    stream = _make_mock_stream(mcu_wire_pb2.PinRead(pin=0))
    await local_svc.AnalogRead(stream)
    sent = stream.sent_message
    assert sent.value == 512

    # 6. PinSubscribe (valid mode)
    service.state.connection_fsm.connect()
    mock_serial.send.return_value = True
    stream = _make_mock_stream(
        mcu_wire_pb2.PinSubscribeRequest(
            pin=13,
            mode=config_schema_pb2.PinModeType.PIN_INPUT_PULLUP,
            interval_ms=100,
            hysteresis=1,
            enabled=True,
        )
    )
    await local_svc.PinSubscribe(stream)
    sent = stream.sent_message
    assert sent.pin == 13
    assert sent.success is True

    # PinSubscribe (invalid mode enum fallback to INPUT)
    stream = _make_mock_stream(
        mcu_wire_pb2.PinSubscribeRequest(
            pin=13,
            mode=9999,
            interval_ms=100,
            hysteresis=1,
            enabled=True,
        )
    )
    await local_svc.PinSubscribe(stream)
    sent = stream.sent_message
    assert sent.pin == 13


async def test_local_bridge_datastore_and_mailbox(tmp_path: Path) -> None:
    service, local_svc, _ = _make_service(_make_config(tmp_path))

    # 1. DatastorePut
    stream = _make_mock_stream(mcu_wire_pb2.DatastorePut(key="mykey", value=b"myval"))
    await local_svc.DatastorePut(stream)
    sent = stream.sent_message
    assert sent.status == "ok"

    # 2. DatastoreGet
    stream = _make_mock_stream(mcu_wire_pb2.DatastoreGet(key="mykey"))
    await local_svc.DatastoreGet(stream)
    sent = stream.sent_message
    assert sent.value == b"myval"

    # 3. MailboxPush
    stream = _make_mock_stream(mcu_wire_pb2.MailboxPush(data=b"test_msg"))
    await local_svc.MailboxPush(stream)
    sent = stream.sent_message
    assert sent.status == "ok"

    # 4. MailboxRead
    await service.state.mailbox_incoming_queue.append(b"incoming_data")
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    await local_svc.MailboxRead(stream)
    sent = stream.sent_message
    assert sent.content == b"incoming_data"


async def test_local_bridge_filesystem(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. Local File Write
    stream = _make_mock_stream(mcu_wire_pb2.FileWrite(path="test.txt", data=b"hello"))
    await local_svc.FileWrite(stream)
    sent = stream.sent_message
    assert sent.status == "ok"

    # 2. Local File Read
    stream = _make_mock_stream(cloud_gateway_pb2.FileRead(path="test.txt"))
    await local_svc.FileRead(stream)
    sent = stream.sent_message
    assert sent.content == b"hello"

    # 3. Local File Remove
    stream = _make_mock_stream(mcu_wire_pb2.FileRemove(path="test.txt"))
    await local_svc.FileRemove(stream)
    sent = stream.sent_message
    assert sent.status == "ok"

    # 4. MCU EEPROM operations use upstream pw_transfer, not unary file RPCs.
    stream = _make_mock_stream(mcu_wire_pb2.FileWrite(path="mcu/eeprom", data=b"data"))
    await local_svc.FileWrite(stream)
    mock_serial.transfer_write.assert_awaited_once_with(0x4D454741, b"data")
    mock_serial.send.assert_not_called()

    stream = _make_mock_stream(mcu_wire_pb2.FileRemove(path="mcu/eeprom"))
    await local_svc.FileRemove(stream)
    mock_serial.send.assert_called_with(
        "rpc.pb.FileSystemService/Remove",
        mcu_wire_pb2.FileRemove(path="eeprom"),
    )

    mock_serial.transfer_read = AsyncMock(return_value=b"eeprom-data")
    stream = _make_mock_stream(cloud_gateway_pb2.FileRead(path="mcu/eeprom"))
    await local_svc.FileRead(stream)
    assert stream.sent_message.content == b"eeprom-data"
    mock_serial.transfer_read.assert_awaited_once_with(0x4D454741)


async def test_local_bridge_process_and_spi(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. ProcessRunAsync (allowed) and Poll
    stream = _make_mock_stream(mcu_wire_pb2.ProcessRunAsync(command="echo hi"))
    await local_svc.ProcessRunAsync(stream)
    sent = stream.sent_message
    assert sent.pid == IsPositiveInt

    # ProcessPoll
    stream = _make_mock_stream(mcu_wire_pb2.ProcessPoll(pid=sent.pid))
    await local_svc.ProcessPoll(stream)
    poll_resp = stream.sent_message
    assert poll_resp is not None

    # ProcessKill (known running process)
    stream_sleep = _make_mock_stream(mcu_wire_pb2.ProcessRunAsync(command="sleep 10"))
    await local_svc.ProcessRunAsync(stream_sleep)
    sent_sleep = stream_sleep.sent_message
    assert sent_sleep.pid == IsPositiveInt

    stream = _make_mock_stream(mcu_wire_pb2.ProcessKill(pid=sent_sleep.pid))
    await local_svc.ProcessKill(stream)
    kill_resp = stream.sent_message
    assert kill_resp.status == "ok"

    # ProcessKill (unknown)
    stream = _make_mock_stream(mcu_wire_pb2.ProcessKill(pid=99999))
    await local_svc.ProcessKill(stream)
    kill_resp = stream.sent_message
    assert kill_resp.status == "error"

    # 2. SPI Transfer & Configure
    mock_serial.send.return_value = mcu_wire_pb2.SpiTransferResponse(data=b"\xaa\x55")
    stream = _make_mock_stream(mcu_wire_pb2.SpiTransfer(data=b"\x01\x02"))
    await local_svc.SpiTransfer(stream)
    sent = stream.sent_message
    assert sent.data == b"\xaa\x55"

    stream = _make_mock_stream(mcu_wire_pb2.SpiConfig(frequency=1000000, bit_order=1, data_mode=0))
    await local_svc.SpiConfigure(stream)
    sent = stream.sent_message
    assert sent.status == "ok"


async def test_local_bridge_telemetry(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. GetVersion (pb object, bytes, no serial, None request)
    mock_serial.send.return_value = mcu_wire_pb2.VersionResponse(major=2, minor=8, patch=5)
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    await local_svc.GetVersion(stream)
    sent = stream.sent_message
    assert sent.major == 2

    mock_serial.send.return_value = 12345  # unexpected type
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    await local_svc.GetVersion(stream)
    sent = stream.sent_message
    assert sent.major == 0

    service.serial = None
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    await local_svc.GetVersion(stream)
    sent = stream.sent_message
    assert sent.major == 0

    await local_svc.GetVersion(_make_mock_stream(None))

    # 2. GetFreeMemory (pb object, bytes, unexpected, no serial, None request)
    service.serial = mock_serial
    mock_serial.send.return_value = mcu_wire_pb2.FreeMemoryResponse(value=1024)
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    await local_svc.GetFreeMemory(stream)
    sent = stream.sent_message
    assert sent.value == 1024

    mock_serial.send.return_value = "invalid"
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    await local_svc.GetFreeMemory(stream)
    sent = stream.sent_message
    assert sent.value == 0

    service.serial = None
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    await local_svc.GetFreeMemory(stream)
    sent = stream.sent_message
    assert sent.value == 0

    await local_svc.GetFreeMemory(_make_mock_stream(None))

    # 3. GetStatus (normal & None request)
    service.serial = mock_serial
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    await local_svc.GetStatus(stream)
    sent = stream.sent_message
    assert sent.metrics is not None

    await local_svc.GetStatus(_make_mock_stream(None))


async def test_local_bridge_edge_branches(tmp_path: Path, mocker: MockerFixture) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # None requests on all remaining RPCs
    await local_svc.DigitalRead(_make_mock_stream(None))
    await local_svc.AnalogWrite(_make_mock_stream(None))
    await local_svc.AnalogRead(_make_mock_stream(None))
    await local_svc.DatastorePut(_make_mock_stream(None))
    await local_svc.DatastoreGet(_make_mock_stream(None))
    await local_svc.MailboxPush(_make_mock_stream(None))
    await local_svc.MailboxRead(_make_mock_stream(None))
    await local_svc.FileWrite(_make_mock_stream(None))
    await local_svc.FileRemove(_make_mock_stream(None))
    await local_svc.ProcessRunAsync(_make_mock_stream(None))
    await local_svc.ProcessPoll(_make_mock_stream(None))
    await local_svc.ProcessKill(_make_mock_stream(None))
    await local_svc.SpiTransfer(_make_mock_stream(None))
    await local_svc.SpiConfigure(_make_mock_stream(None))
    await local_svc.Publish(_make_mock_stream(None))
    await local_svc.SubscribeConsole(_make_mock_stream(None))

    # AnalogRead branches
    mock_serial.send.return_value = mcu_wire_pb2.AnalogReadResponse(value=100)
    stream = _make_mock_stream(mcu_wire_pb2.PinRead(pin=0))
    await local_svc.AnalogRead(stream)
    assert stream.sent_message.value == 100

    mock_serial.send.return_value = 999
    stream = _make_mock_stream(mcu_wire_pb2.PinRead(pin=0))
    await local_svc.AnalogRead(stream)
    assert stream.sent_message.value == 0

    service.serial = None
    stream = _make_mock_stream(mcu_wire_pb2.PinRead(pin=0))
    await local_svc.AnalogRead(stream)
    assert stream.sent_message.value == 0

    # DigitalRead unexpected object branch
    mock_serial.send.return_value = 999
    service.serial = mock_serial
    stream = _make_mock_stream(mcu_wire_pb2.PinRead(pin=1))
    await local_svc.DigitalRead(stream)
    assert stream.sent_message.value == 0

    # Datastore cache None branch
    service.state.datastore_cache = None
    stream = _make_mock_stream(mcu_wire_pb2.DatastorePut(key="k", value=b"v"))
    await local_svc.DatastorePut(stream)
    stream = _make_mock_stream(mcu_wire_pb2.DatastoreGet(key="k"))
    await local_svc.DatastoreGet(stream)
    assert stream.sent_message.value == b""

    # MailboxRead empty queue branch
    await service.state.mailbox_incoming_queue.clear()
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    await local_svc.MailboxRead(stream)
    assert stream.sent_message.content == b""

    # File operations with no serial on MCU path
    service.serial = None
    stream = _make_mock_stream(mcu_wire_pb2.FileWrite(path="mcu/test.bin", data=b"abc"))
    await local_svc.FileWrite(stream)
    assert stream.sent_message.status == "error"

    stream = _make_mock_stream(mcu_wire_pb2.FileRemove(path="mcu/test.bin"))
    await local_svc.FileRemove(stream)
    assert stream.sent_message.status == "error"

    # File operations on path traversal (escapes root via ../)
    stream = _make_mock_stream(mcu_wire_pb2.FileWrite(path="../../etc/shadow", data=b"bad"))
    await local_svc.FileWrite(stream)
    assert stream.sent_message.status == "error"

    stream = _make_mock_stream(mcu_wire_pb2.FileRemove(path="../../etc/nonexistent_file_abc"))
    await local_svc.FileRemove(stream)
    assert stream.sent_message.status == "error"

    # SPI branches (bytes, unexpected, no serial)
    mock_serial.send.return_value = mcu_wire_pb2.SpiTransferResponse(data=b"\x01")
    service.serial = mock_serial
    stream = _make_mock_stream(mcu_wire_pb2.SpiTransfer(data=b"\x02"))
    await local_svc.SpiTransfer(stream)
    assert stream.sent_message.data == b"\x01"

    mock_serial.send.return_value = None
    stream = _make_mock_stream(mcu_wire_pb2.SpiTransfer(data=b"\x02"))
    await local_svc.SpiTransfer(stream)
    assert stream.sent_message.data == b""

    service.serial = None
    stream = _make_mock_stream(mcu_wire_pb2.SpiTransfer(data=b"\x02"))
    await local_svc.SpiTransfer(stream)
    assert stream.sent_message.data == b""

    stream = _make_mock_stream(mcu_wire_pb2.SpiConfig(frequency=1000))
    await local_svc.SpiConfigure(stream)
    assert stream.sent_message.status == "error"

    # ProcessRunAsync disallowed command (pid=0 for denied)
    stream = _make_mock_stream(mcu_wire_pb2.ProcessRunAsync(command="rm -rf /"))
    await local_svc.ProcessRunAsync(stream)
    assert stream.sent_message.pid == 0

    # ProcessKill exception branch
    mock_handle = mocker.MagicMock()
    mock_handle.returncode = None
    type(mock_handle).pid = mocker.PropertyMock(side_effect=OSError("Kill failed"))
    service.state.running_processes[8888] = ProcessContext(
        handle=mock_handle,
    )
    stream = _make_mock_stream(mcu_wire_pb2.ProcessKill(pid=8888))
    await local_svc.ProcessKill(stream)
    assert stream.sent_message.status == "error"


async def test_local_bridge_publish_and_console(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. Publish non-query message (returns empty ack CloudQueuedPublish)
    stream = _make_mock_stream(cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/custom/test", payload=b"payload"))
    await local_svc.Publish(stream)
    sent = stream.sent_message
    assert isinstance(sent, cloud_gateway_pb2.CloudQueuedPublish)
    # Non-query messages return an empty ack (no topic echoed back)
    assert sent.topic_name == ""

    # 2. Publish query with correlation (simulate immediate reply)
    service.handle_request = AsyncMock()
    stream = _make_mock_stream(
        cloud_gateway_pb2.CloudQueuedPublish(
            topic_name="br/d/13/get",
            correlation_data=b"test_corr_12",
        )
    )

    async def _fulfill():
        await asyncio.sleep(0.01)
        queue = service.ipc_requests.get(b"test_corr_12")
        if queue:
            await queue.put(cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/d/13/value", payload=b"1"))

    asyncio.create_task(_fulfill())
    await local_svc.Publish(stream)
    sent = stream.sent_message
    assert sent.payload == b"1"

    # 3. SubscribeConsole streaming — uses console_queues, not console_incoming_queue.
    # SubscribeConsole registers a fresh asyncio.Queue in service.console_queues,
    # then blocks reading from it. We push a CloudQueuedPublish to that queue,
    # then trigger an OSError to break the loop (OSError is caught & re-raised).
    async def _push_console() -> None:
        await asyncio.sleep(0.05)
        # The SubscribeConsole method appends its queue to console_queues
        if service.console_queues:
            q = service.console_queues[-1]
            await q.put(cloud_gateway_pb2.CloudQueuedPublish(topic_name="console", payload=b"MCU log\n"))
            await asyncio.sleep(0.05)
            stream.send_message.side_effect = OSError("stream closed")
            await q.put(cloud_gateway_pb2.CloudQueuedPublish())  # Trigger next iteration

    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())
    asyncio.create_task(_push_console())
    with pytest.raises(OSError):
        await local_svc.SubscribeConsole(stream)


async def test_local_bridge_execute_rpc(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. DigitalWrite
    mock_serial.send.return_value = True
    req_dw = mcu_wire_pb2.DigitalWrite(pin=13, value=1)
    res_dw_bytes = await local_svc.execute_rpc("DigitalWrite", req_dw.SerializeToString())
    res_dw = cloud_gateway_pb2.GenericResponse.FromString(res_dw_bytes)
    assert res_dw.status == "ok"

    mock_serial.send.return_value = False
    res_dw_failed_bytes = await local_svc.execute_rpc("DigitalWrite", req_dw.SerializeToString())
    assert cloud_gateway_pb2.GenericResponse.FromString(res_dw_failed_bytes).status == "error"

    mock_serial.send.return_value = True

    # 2. DigitalRead
    mock_serial.send.return_value = mcu_wire_pb2.DigitalReadResponse(value=1)
    req_dr = mcu_wire_pb2.PinRead(pin=13)
    res_dr_bytes = await local_svc.execute_rpc("DigitalRead", req_dr.SerializeToString())
    res_dr = mcu_wire_pb2.DigitalReadResponse.FromString(res_dr_bytes)
    assert res_dr.value == 1

    # 3. Datastore Put & Get
    req_ds_put = mcu_wire_pb2.DatastorePut(key="k1", value=b"v1")
    res_put_bytes = await local_svc.execute_rpc("DatastorePut", req_ds_put.SerializeToString())
    assert cloud_gateway_pb2.GenericResponse.FromString(res_put_bytes).status == "ok"

    req_ds_get = mcu_wire_pb2.DatastoreGet(key="k1")
    res_get_bytes = await local_svc.execute_rpc("DatastoreGet", req_ds_get.SerializeToString())
    assert mcu_wire_pb2.DatastoreGetResponse.FromString(res_get_bytes).value == b"v1"

    # 4. Unknown method raises ValueError
    with pytest.raises(ValueError, match="Unknown RPC method"):
        await local_svc.execute_rpc("NonExistent", b"")


async def test_local_bridge_write_methods_report_failed_serial_send(tmp_path: Path) -> None:
    _, local_svc, mock_serial = _make_service(_make_config(tmp_path))
    mock_serial.send.return_value = False

    pin_mode_response = cloud_gateway_pb2.GenericResponse.FromString(
        await local_svc.execute_rpc(
            "SetPinMode", mcu_wire_pb2.PinMode(pin=13, mode=config_schema_pb2.PIN_OUTPUT).SerializeToString()
        )
    )
    digital_write_response = cloud_gateway_pb2.GenericResponse.FromString(
        await local_svc.execute_rpc("DigitalWrite", mcu_wire_pb2.DigitalWrite(pin=13, value=1).SerializeToString())
    )
    analog_write_response = cloud_gateway_pb2.GenericResponse.FromString(
        await local_svc.execute_rpc("AnalogWrite", mcu_wire_pb2.AnalogWrite(pin=9, value=128).SerializeToString())
    )

    assert pin_mode_response.status == "error"
    assert digital_write_response.status == "error"
    assert analog_write_response.status == "error"
    assert mock_serial.send.await_count == 3


async def test_local_bridge_service_ipc(tmp_path: Path) -> None:
    svc, local_svc, _ = _make_service(_make_config(tmp_path))

    # Test Publish with no stream message
    stream_none = MockGrpcStream(request=None)
    await local_svc.Publish(stream_none.as_stream())
    stream_none.send_message.assert_not_called()

    req_msg = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name="br/req/DataStoreService/Put",
        payload=mcu_wire_pb2.DatastorePut(key="test_key", value=b"val").SerializeToString(),
        correlation_data=b"123456789012",
    )
    mock_stream = MockGrpcStream(request=req_msg)
    await local_svc.Publish(mock_stream.as_stream())
    mock_stream.send_message.assert_awaited_once()
    sent_msg = mock_stream.sent_message
    assert isinstance(sent_msg, cloud_gateway_pb2.CloudQueuedPublish)
    assert sent_msg.correlation_data == b"123456789012"


async def test_local_bridge_service_subscribe_console(tmp_path: Path) -> None:
    svc, local_svc, _ = _make_service(_make_config(tmp_path))

    mock_stream = MockGrpcStream(
        request=mcu_wire_pb2.SubscribeRequest(),
        send_exception=OSError("Connection reset"),
    )

    q_msg = cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/console/out", payload=b"hello console")

    async def _push() -> None:
        await asyncio.sleep(0.01)
        if svc.console_queues:
            await svc.console_queues[0].put(q_msg)

    asyncio.create_task(_push())
    with pytest.raises(OSError):
        await local_svc.SubscribeConsole(mock_stream.as_stream())


async def test_local_bridge_service_subscribe_console_normal_close_and_adapter_cleanup(tmp_path: Path) -> None:
    svc, local_svc, _ = _make_service(_make_config(tmp_path))
    stream = _make_mock_stream(mcu_wire_pb2.SubscribeRequest())

    async def _producer() -> None:
        await asyncio.sleep(0.01)
        if svc.console_queues:
            adapter = svc.console_queues[-1]
            await adapter.put(
                cloud_gateway_pb2.CloudQueuedPublish(topic_name="br/console/out", payload=b"normal close")
            )
            svc.console_queues.remove(adapter)
            adapter.close()

    asyncio.create_task(_producer())
    await local_svc.SubscribeConsole(stream)
    assert stream.sent_message.payload == b"normal close"


async def test_local_bridge_service_publish_timeout_and_oserror(tmp_path: Path, mocker: MockerFixture) -> None:
    svc, local_svc, _ = _make_service(_make_config(tmp_path))

    req_msg = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name="br/file/read",
        payload=b"test",
        correlation_data=b"corr-timeout-1",
    )
    mock_stream = MockGrpcStream(request=req_msg)

    _orig_timeout = asyncio.timeout

    def _quick_timeout(_seconds: float) -> Any:
        return _orig_timeout(0.001)

    mocker.patch("mcubridge.services.runtime.asyncio.timeout", side_effect=_quick_timeout)
    await local_svc.Publish(mock_stream.as_stream())
    assert mock_stream.send_message.called

    # 2. Simulate OSError during response write
    req_put = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name="br/datastore/put/test_key",
        payload=b"val",
        correlation_data=b"corr-timeout-1",
    )
    mock_stream_broken = MockGrpcStream(
        request=req_put,
        send_exception=OSError("Socket broken"),
    )

    await local_svc.Publish(mock_stream_broken.as_stream())
    assert b"corr-timeout-1" not in svc.ipc_requests


def _make_property_service() -> tuple[BridgeService, MockLocalBridgeClient, AsyncMock]:
    cfg = RuntimeConfig()
    cfg.allowed_policy.entries.extend(["echo", "sleep", "test_cmd"])
    cfg.allow_non_tmp_paths = True
    state = create_runtime_state(cfg)
    mock_serial = AsyncMock()
    mock_serial.send = AsyncMock(return_value=True)
    service = BridgeService(config=cfg, state=state, serial=mock_serial)
    return service, MockLocalBridgeClient(service), mock_serial


@given(
    pin_mode=st.from_type(mcu_wire_pb2.PinMode),
    digital_write=st.from_type(mcu_wire_pb2.DigitalWrite),
    analog_write=st.from_type(mcu_wire_pb2.AnalogWrite),
)
async def test_local_bridge_pin_operations_property(
    pin_mode: mcu_wire_pb2.PinMode,
    digital_write: mcu_wire_pb2.DigitalWrite,
    analog_write: mcu_wire_pb2.AnalogWrite,
) -> None:
    service, local_svc, mock_serial = _make_property_service()
    try:
        # SetPinMode
        stream_mode = _make_mock_stream(pin_mode)
        await local_svc.SetPinMode(stream_mode.as_stream())
        assert stream_mode.sent_message is not None
        assert stream_mode.sent_message.status == "ok"
        mock_serial.send.assert_called_with(protocol.get_operation("SetPinMode"), pin_mode)

        # DigitalWrite
        stream_dw = _make_mock_stream(digital_write)
        await local_svc.DigitalWrite(stream_dw.as_stream())
        assert stream_dw.sent_message is not None
        assert stream_dw.sent_message.status == "ok"
        mock_serial.send.assert_called_with(protocol.get_operation("DigitalWrite"), digital_write)

        # AnalogWrite
        stream_aw = _make_mock_stream(analog_write)
        await local_svc.AnalogWrite(stream_aw.as_stream())
        assert stream_aw.sent_message is not None
        assert stream_aw.sent_message.status == "ok"
        mock_serial.send.assert_called_with(protocol.get_operation("AnalogWrite"), analog_write)
    finally:
        service.state.cleanup()


@given(
    datastore_put=st.from_type(mcu_wire_pb2.DatastorePut),
    mailbox_push=st.from_type(mcu_wire_pb2.MailboxPush),
)
async def test_local_bridge_datastore_and_mailbox_property(
    datastore_put: mcu_wire_pb2.DatastorePut,
    mailbox_push: mcu_wire_pb2.MailboxPush,
) -> None:
    service, local_svc, _mock_serial = _make_property_service()
    try:
        # DatastorePut
        stream_dp = _make_mock_stream(datastore_put)
        await local_svc.DatastorePut(stream_dp.as_stream())
        assert stream_dp.sent_message is not None
        assert stream_dp.sent_message.status == "ok"

        # DatastoreGet roundtrip from cache
        stream_dg = _make_mock_stream(mcu_wire_pb2.DatastoreGet(key=datastore_put.key))
        await local_svc.DatastoreGet(stream_dg.as_stream())
        assert stream_dg.sent_message is not None
        assert stream_dg.sent_message.value == datastore_put.value

        # MailboxPush
        stream_mp = _make_mock_stream(mailbox_push)
        await local_svc.MailboxPush(stream_mp.as_stream())
        assert stream_mp.sent_message is not None
        assert stream_mp.sent_message.status == "ok"
        assert len(service.state.mailbox_queue) >= 1
    finally:
        service.state.cleanup()


@given(
    spi_req=st.from_type(mcu_wire_pb2.SpiTransfer),
)
async def test_local_bridge_spi_transfer_property(
    spi_req: mcu_wire_pb2.SpiTransfer,
) -> None:
    service, local_svc, mock_serial = _make_property_service()
    try:
        mock_serial.send.return_value = mcu_wire_pb2.SpiTransferResponse(
            data=spi_req.data,
        )
        stream_spi = _make_mock_stream(spi_req)
        await local_svc.SpiTransfer(stream_spi.as_stream())
        assert stream_spi.sent_message is not None
        assert stream_spi.sent_message.data == spi_req.data
        mock_serial.send.assert_called_with(protocol.get_operation("SpiTransfer"), spi_req)
    finally:
        service.state.cleanup()
