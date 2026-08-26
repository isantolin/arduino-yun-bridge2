"""Unit tests for LocalBridgeService typed gRPC methods. [SIL-2]"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from grpclib.server import Stream

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command
from mcubridge.services.runtime import BridgeService, LocalBridgeService
from mcubridge.state.context import ProcessContext, create_runtime_state


def _make_config(tmp_path: Path) -> RuntimeConfig:
    cfg = RuntimeConfig()
    cfg.file_system_root = str(tmp_path)
    cfg.allowed_policy.entries.extend(["echo", "sleep", "test_cmd"])
    cfg.allow_non_tmp_paths = True
    return cfg


def _make_service(config: RuntimeConfig) -> tuple[BridgeService, LocalBridgeService, AsyncMock]:
    state = create_runtime_state(config)
    mock_serial = AsyncMock()
    mock_serial.send = AsyncMock(return_value=True)
    service = BridgeService(config=config, state=state, serial=mock_serial)
    local_service = LocalBridgeService(service)
    return service, local_service, mock_serial


def _make_mock_stream(req_msg: object) -> MagicMock:
    stream = MagicMock(spec=Stream)
    stream.recv_message = AsyncMock(return_value=req_msg)
    stream.send_message = AsyncMock()
    return stream


@pytest.mark.asyncio
async def test_local_bridge_pin_operations(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. SetPinMode
    stream = _make_mock_stream(pb.PinMode(pin=13, mode=pb.PIN_OUTPUT))
    await local_svc.SetPinMode(stream)
    mock_serial.send.assert_called_with(Command.CMD_SET_PIN_MODE.value, pb.PinMode(pin=13, mode=pb.PIN_OUTPUT))
    sent = stream.send_message.call_args[0][0]
    assert sent.status == "ok"

    # None request
    stream_none = _make_mock_stream(None)
    await local_svc.SetPinMode(stream_none)

    # 2. DigitalWrite
    stream = _make_mock_stream(pb.DigitalWrite(pin=13, value=1))
    await local_svc.DigitalWrite(stream)
    mock_serial.send.assert_called_with(Command.CMD_DIGITAL_WRITE.value, pb.DigitalWrite(pin=13, value=1))
    sent = stream.send_message.call_args[0][0]
    assert sent.status == "ok"

    # 3. DigitalRead (direct object)
    mock_serial.send.return_value = pb.DigitalReadResponse(value=1)
    stream = _make_mock_stream(pb.PinRead(pin=13))
    await local_svc.DigitalRead(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.value == 1

    # DigitalRead (bytes serialization)
    mock_serial.send.return_value = pb.DigitalReadResponse(value=0).SerializeToString()
    stream = _make_mock_stream(pb.PinRead(pin=13))
    await local_svc.DigitalRead(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.value == 0

    # DigitalRead (no serial)
    service.serial = None
    stream = _make_mock_stream(pb.PinRead(pin=13))
    await local_svc.DigitalRead(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.value == 0

    # 4. AnalogWrite
    service.serial = mock_serial
    mock_serial.send.return_value = True
    stream = _make_mock_stream(pb.AnalogWrite(pin=9, value=128))
    await local_svc.AnalogWrite(stream)
    mock_serial.send.assert_called_with(Command.CMD_ANALOG_WRITE.value, pb.AnalogWrite(pin=9, value=128))
    sent = stream.send_message.call_args[0][0]
    assert sent.status == "ok"

    # 5. AnalogRead
    mock_serial.send.return_value = pb.AnalogReadResponse(value=512)
    stream = _make_mock_stream(pb.PinRead(pin=0))
    await local_svc.AnalogRead(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.value == 512


@pytest.mark.asyncio
async def test_local_bridge_datastore_and_mailbox(tmp_path: Path) -> None:
    service, local_svc, _ = _make_service(_make_config(tmp_path))

    # 1. DatastorePut
    stream = _make_mock_stream(pb.DatastorePut(key="mykey", value=b"myval"))
    await local_svc.DatastorePut(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.status == "ok"

    # 2. DatastoreGet
    stream = _make_mock_stream(pb.DatastoreGet(key="mykey"))
    await local_svc.DatastoreGet(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.value == b"myval"

    # 3. MailboxPush
    stream = _make_mock_stream(pb.MailboxPush(data=b"test_msg"))
    await local_svc.MailboxPush(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.status == "ok"

    # 4. MailboxRead
    await service.state.mailbox_incoming_queue.append(b"incoming_data")
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.MailboxRead(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.content == b"incoming_data"


@pytest.mark.asyncio
async def test_local_bridge_filesystem(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. Local File Write
    stream = _make_mock_stream(pb.FileWrite(path="test.txt", data=b"hello"))
    await local_svc.FileWrite(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.status == "ok"

    # 2. Local File Read
    stream = _make_mock_stream(pb.FileRead(path="test.txt"))
    await local_svc.FileRead(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.content == b"hello"

    # 3. Local File Remove
    stream = _make_mock_stream(pb.FileRemove(path="test.txt"))
    await local_svc.FileRemove(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.status == "ok"

    # 4. MCU File Operations
    stream = _make_mock_stream(pb.FileWrite(path="mcu/eeprom.bin", data=b"data"))
    await local_svc.FileWrite(stream)
    mock_serial.send.assert_called_with(Command.CMD_FILE_WRITE.value, pb.FileWrite(path="eeprom.bin", data=b"data"))

    mock_serial.send.return_value = pb.FileReadResponse(content=b"mcu_data")
    stream = _make_mock_stream(pb.FileRead(path="mcu/eeprom.bin"))
    await local_svc.FileRead(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.content == b"mcu_data"

    stream = _make_mock_stream(pb.FileRemove(path="mcu/eeprom.bin"))
    await local_svc.FileRemove(stream)
    mock_serial.send.assert_called_with(Command.CMD_FILE_REMOVE.value, pb.FileRemove(path="eeprom.bin"))


@pytest.mark.asyncio
async def test_local_bridge_process_and_spi(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. ProcessRunAsync (allowed) and Poll
    stream = _make_mock_stream(pb.ProcessRunAsync(command="echo hi"))
    await local_svc.ProcessRunAsync(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.pid > 0

    # ProcessPoll
    stream = _make_mock_stream(pb.ProcessPoll(pid=sent.pid))
    await local_svc.ProcessPoll(stream)
    poll_resp = stream.send_message.call_args[0][0]
    assert poll_resp is not None

    # ProcessKill (known running process)
    stream_sleep = _make_mock_stream(pb.ProcessRunAsync(command="sleep 10"))
    await local_svc.ProcessRunAsync(stream_sleep)
    sent_sleep = stream_sleep.send_message.call_args[0][0]
    assert sent_sleep.pid > 0

    stream = _make_mock_stream(pb.ProcessKill(pid=sent_sleep.pid))
    await local_svc.ProcessKill(stream)
    kill_resp = stream.send_message.call_args[0][0]
    assert kill_resp.status == "ok"

    # ProcessKill (unknown)
    stream = _make_mock_stream(pb.ProcessKill(pid=99999))
    await local_svc.ProcessKill(stream)
    kill_resp = stream.send_message.call_args[0][0]
    assert kill_resp.status == "error"

    # 2. SPI Transfer & Configure
    mock_serial.send.return_value = pb.SpiTransferResponse(data=b"\xaa\x55")
    stream = _make_mock_stream(pb.SpiTransfer(data=b"\x01\x02"))
    await local_svc.SpiTransfer(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.data == b"\xaa\x55"

    stream = _make_mock_stream(pb.SpiConfig(frequency=1000000, bit_order=1, data_mode=0))
    await local_svc.SpiConfigure(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.status == "ok"


@pytest.mark.asyncio
async def test_local_bridge_telemetry(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. GetVersion (pb object, bytes, no serial, None request)
    mock_serial.send.return_value = pb.VersionResponse(major=2, minor=8, patch=5)
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.GetVersion(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.major == 2

    mock_serial.send.return_value = pb.VersionResponse(major=2, minor=8, patch=6).SerializeToString()
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.GetVersion(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.minor == 8

    mock_serial.send.return_value = 12345  # unexpected type
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.GetVersion(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.major == 0

    service.serial = None
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.GetVersion(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.major == 0

    await local_svc.GetVersion(_make_mock_stream(None))

    # 2. GetFreeMemory (pb object, bytes, unexpected, no serial, None request)
    service.serial = mock_serial
    mock_serial.send.return_value = pb.FreeMemoryResponse(value=1024)
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.GetFreeMemory(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.value == 1024

    mock_serial.send.return_value = pb.FreeMemoryResponse(value=2048).SerializeToString()
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.GetFreeMemory(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.value == 2048

    mock_serial.send.return_value = "invalid"
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.GetFreeMemory(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.value == 0

    service.serial = None
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.GetFreeMemory(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.value == 0

    await local_svc.GetFreeMemory(_make_mock_stream(None))

    # 3. GetStatus (normal & None request)
    service.serial = mock_serial
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.GetStatus(stream)
    sent = stream.send_message.call_args[0][0]
    assert sent.metrics is not None

    await local_svc.GetStatus(_make_mock_stream(None))


@pytest.mark.asyncio
async def test_local_bridge_edge_branches(tmp_path: Path) -> None:
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
    await local_svc.FileRead(_make_mock_stream(None))
    await local_svc.FileRemove(_make_mock_stream(None))
    await local_svc.ProcessRunAsync(_make_mock_stream(None))
    await local_svc.ProcessPoll(_make_mock_stream(None))
    await local_svc.ProcessKill(_make_mock_stream(None))
    await local_svc.SpiTransfer(_make_mock_stream(None))
    await local_svc.SpiConfigure(_make_mock_stream(None))
    await local_svc.Publish(_make_mock_stream(None))
    await local_svc.SubscribeConsole(_make_mock_stream(None))

    # AnalogRead branches
    mock_serial.send.return_value = pb.AnalogReadResponse(value=100).SerializeToString()
    stream = _make_mock_stream(pb.PinRead(pin=0))
    await local_svc.AnalogRead(stream)
    assert stream.send_message.call_args[0][0].value == 100

    mock_serial.send.return_value = 999
    stream = _make_mock_stream(pb.PinRead(pin=0))
    await local_svc.AnalogRead(stream)
    assert stream.send_message.call_args[0][0].value == 0

    service.serial = None
    stream = _make_mock_stream(pb.PinRead(pin=0))
    await local_svc.AnalogRead(stream)
    assert stream.send_message.call_args[0][0].value == 0

    # DigitalRead unexpected object branch
    mock_serial.send.return_value = 999
    service.serial = mock_serial
    stream = _make_mock_stream(pb.PinRead(pin=1))
    await local_svc.DigitalRead(stream)
    assert stream.send_message.call_args[0][0].value == 0

    # Datastore cache None branch
    service.state.datastore_cache = None
    stream = _make_mock_stream(pb.DatastorePut(key="k", value=b"v"))
    await local_svc.DatastorePut(stream)
    stream = _make_mock_stream(pb.DatastoreGet(key="k"))
    await local_svc.DatastoreGet(stream)
    assert stream.send_message.call_args[0][0].value == b""

    # MailboxRead empty queue branch
    await service.state.mailbox_incoming_queue.clear()
    stream = _make_mock_stream(pb.SubscribeRequest())
    await local_svc.MailboxRead(stream)
    assert stream.send_message.call_args[0][0].content == b""

    # File operations with no serial on MCU path
    service.serial = None
    stream = _make_mock_stream(pb.FileWrite(path="mcu/test.bin", data=b"abc"))
    await local_svc.FileWrite(stream)
    assert stream.send_message.call_args[0][0].status == "error"

    stream = _make_mock_stream(pb.FileRead(path="mcu/test.bin"))
    await local_svc.FileRead(stream)
    assert stream.send_message.call_args[0][0].content == b""

    stream = _make_mock_stream(pb.FileRemove(path="mcu/test.bin"))
    await local_svc.FileRemove(stream)
    assert stream.send_message.call_args[0][0].status == "error"

    # File operations on path traversal (escapes root via ../)
    stream = _make_mock_stream(pb.FileWrite(path="../../etc/shadow", data=b"bad"))
    await local_svc.FileWrite(stream)
    assert stream.send_message.call_args[0][0].status == "error"

    stream = _make_mock_stream(pb.FileRead(path="../../etc/nonexistent_file_abc"))
    await local_svc.FileRead(stream)
    assert stream.send_message.call_args[0][0].content == b""

    stream = _make_mock_stream(pb.FileRemove(path="../../etc/nonexistent_file_abc"))
    await local_svc.FileRemove(stream)
    assert stream.send_message.call_args[0][0].status == "error"

    # SPI branches (bytes, unexpected, no serial)
    mock_serial.send.return_value = pb.SpiTransferResponse(data=b"\x01").SerializeToString()
    service.serial = mock_serial
    stream = _make_mock_stream(pb.SpiTransfer(data=b"\x02"))
    await local_svc.SpiTransfer(stream)
    assert stream.send_message.call_args[0][0].data == b"\x01"

    mock_serial.send.return_value = None
    stream = _make_mock_stream(pb.SpiTransfer(data=b"\x02"))
    await local_svc.SpiTransfer(stream)
    assert stream.send_message.call_args[0][0].data == b""

    service.serial = None
    stream = _make_mock_stream(pb.SpiTransfer(data=b"\x02"))
    await local_svc.SpiTransfer(stream)
    assert stream.send_message.call_args[0][0].data == b""

    stream = _make_mock_stream(pb.SpiConfig(frequency=1000))
    await local_svc.SpiConfigure(stream)
    assert stream.send_message.call_args[0][0].status == "error"

    # ProcessRunAsync disallowed command (pid=0 for denied)
    stream = _make_mock_stream(pb.ProcessRunAsync(command="rm -rf /"))
    await local_svc.ProcessRunAsync(stream)
    assert stream.send_message.call_args[0][0].pid == 0

    # ProcessKill exception branch
    service.state.running_processes[8888] = ProcessContext(
        handle=MagicMock(),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "_terminate_process", AsyncMock(side_effect=OSError("Kill failed")))
        stream = _make_mock_stream(pb.ProcessKill(pid=8888))
        await local_svc.ProcessKill(stream)
        assert stream.send_message.call_args[0][0].status == "error"


@pytest.mark.asyncio
async def test_local_bridge_publish_and_console(tmp_path: Path) -> None:
    service, local_svc, mock_serial = _make_service(_make_config(tmp_path))

    # 1. Publish non-query message (returns empty ack CloudQueuedPublish)
    stream = _make_mock_stream(pb.CloudQueuedPublish(topic_name="br/custom/test", payload=b"payload"))
    await local_svc.Publish(stream)
    sent = stream.send_message.call_args[0][0]
    assert isinstance(sent, pb.CloudQueuedPublish)
    # Non-query messages return an empty ack (no topic echoed back)
    assert sent.topic_name == ""

    # 2. Publish query with correlation (simulate immediate reply)
    service.handle_request = AsyncMock()
    stream = _make_mock_stream(
        pb.CloudQueuedPublish(
            topic_name="br/d/13/get",
            correlation_data=b"test_corr_12",
        )
    )

    async def _fulfill():
        await asyncio.sleep(0.01)
        queue = service.ipc_requests.get(b"test_corr_12")
        if queue:
            await queue.put(pb.CloudQueuedPublish(topic_name="br/d/13/value", payload=b"1"))

    asyncio.create_task(_fulfill())
    await local_svc.Publish(stream)
    sent = stream.send_message.call_args[0][0]
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
            await q.put(pb.CloudQueuedPublish(topic_name="console", payload=b"MCU log\n"))
            await asyncio.sleep(0.05)
            stream.send_message.side_effect = OSError("stream closed")
            await q.put(pb.CloudQueuedPublish())  # Trigger next iteration

    stream = _make_mock_stream(pb.SubscribeRequest())
    asyncio.create_task(_push_console())
    with pytest.raises(OSError):
        await local_svc.SubscribeConsole(stream)
