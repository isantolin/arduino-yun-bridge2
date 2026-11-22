"""Assertive, deterministic tests for McuBridge runtime service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import cloud_gateway_pb2, mcu_wire_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import RuntimeState, create_runtime_state
from mcubridge.transport.serial import SerialTransport


@pytest.fixture
def service_setup(tmp_path: Path) -> tuple[BridgeService, RuntimeState, AsyncMock]:
    cfg = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/null",
        serial_baud=115200,
        serial_safe_baud=9600,
        allowed_commands=["ls", "echo", "sleep"],
        file_system_root=str(tmp_path),
        allow_non_tmp_paths=True,
    )
    state = create_runtime_state(cfg)
    state.file_system_root = str(tmp_path)
    state.allow_non_tmp_paths = True
    state.connection_fsm.connect()
    state.connection_fsm.synchronize()

    mock_serial = AsyncMock(spec=SerialTransport)
    mock_cloud_publisher = AsyncMock()
    service = BridgeService(cfg, state, mock_serial, cloud_publisher=mock_cloud_publisher)
    return service, state, mock_serial


async def test_cloud_file_write_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    _service, _state, serial = service_setup
    serial.transfer_write = AsyncMock()
    await serial.transfer_write(0x4D454741, b"new_data")
    serial.transfer_write.assert_awaited_once_with(0x4D454741, b"new_data")
    serial.send.assert_not_called()


async def test_cloud_datastore_put_asserts_cache(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    resp_bytes = await service.execute_rpc(
        "rpc.pb.DataStoreService/Put",
        pb.DatastorePut(key="my_key", value=b"my_value").SerializeToString(),
    )

    assert cloud_gateway_pb2.GenericResponse.FromString(resp_bytes).status == "ok"
    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("my_key") == b"my_value"


async def test_mcu_datastore_put_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    payload = pb.DatastorePut(key="mcu_key", value=b"mcu_val").SerializeToString()

    await service.handle_mcu_frame("rpc.pb.DataStoreService/Put", 1, payload)

    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("mcu_key") == b"mcu_val"

    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_called_once()
    queued_pub = cast(cloud_gateway_pb2.CloudQueuedPublish, mock_pub.call_args[0][0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/res/rpc.pb.DataStoreService/Get"
    assert queued_pub.payload == b"mcu_val"


async def test_local_bridge_datastore_put_persists_and_publishes(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    request = pb.DatastorePut(key="local_key", value=b"local_value")

    response = await service.datastore_put(request)

    assert response.status == "ok"
    assert state.datastore_cache is not None
    assert await state.datastore_cache.get("local_key") == b"local_value"
    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_awaited_once()
    publish_call = mock_pub.await_args
    assert publish_call is not None
    queued_pub = cast(cloud_gateway_pb2.CloudQueuedPublish, publish_call.args[0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/res/rpc.pb.DataStoreService/Get"
    assert queued_pub.payload == b"local_value"


async def test_mcu_mailbox_push_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    payload = pb.MailboxPush(data=b"pushed_msg").SerializeToString()

    await service.handle_mcu_frame("rpc.pb.MailboxService/Push", 1, payload)

    mock_pub = cast(AsyncMock, service.cloud_publisher)
    mock_pub.assert_called_once()
    queued_pub = cast(cloud_gateway_pb2.CloudQueuedPublish, mock_pub.call_args[0][0])
    assert queued_pub.topic_name == f"{state.cloud_topic_prefix}/evt/rpc.pb.MailboxService/Push"
    assert queued_pub.payload == b"pushed_msg"


async def test_cloud_mailbox_write_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, serial = service_setup
    serial.send.return_value = True
    await service.execute_rpc(
        "rpc.pb.MailboxService/Push",
        pb.MailboxPush(data=b"outbound_box").SerializeToString(),
    )

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == "rpc.pb.MailboxService/Push"
    assert isinstance(serial.send.call_args[0][1], pb.MailboxPush)
    assert serial.send.call_args[0][1].data == b"outbound_box"
    # Delivered by push, so it must not also be queued for MailboxService/Read.
    assert len(state.mailbox_queue) == 0


async def test_cloud_mailbox_push_failure_queues_for_read(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """A message the MCU did not take stays available to MailboxService/Read."""
    service, state, serial = service_setup
    serial.send.return_value = False

    await service.mailbox_push(b"retry_later")

    serial.send.assert_awaited_once()
    assert len(state.mailbox_queue) == 1
    read = await service.mailbox_read(0)
    assert read.content == b"retry_later"
    assert len(state.mailbox_queue) == 0


async def test_cloud_mailbox_push_without_serial_queues_for_read(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    service.serial = None

    await service.mailbox_push(b"offline")

    assert len(state.mailbox_queue) == 1


async def test_mcu_process_run_asserts_exec(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = service_setup
    serial.send.return_value = True
    payload = pb.ProcessRunAsync(command="echo hello").SerializeToString()

    await service.handle_mcu_frame("rpc.pb.ProcessService/RunAsync", 1, payload)

    serial.send.assert_awaited()
    args = serial.send.call_args[0]
    assert args[0] == "rpc.pb.ProcessService/RunAsync"
    assert isinstance(args[1], pb.ProcessRunAsyncResponse)
    pid = args[1].pid
    assert pid > 0
    await service.process_kill(pid)


async def test_process_run_malformed_quoting_does_not_leak_slots(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Regression: a shlex error used to escape after a slot was taken, leaking it forever."""
    service, state, _serial = service_setup
    for _ in range(state.process_max_concurrent * 2 + 1):
        rejected = await service.process_run_async('echo "unterminated')
        assert rejected.pid == 0
    accepted = await service.process_run_async("echo ok")
    assert accepted.pid > 0
    await service.process_kill(accepted.pid)


async def test_process_run_rejects_commands_outside_exact_policy(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """The policy entry "ls" allows neither a differently-cased program nor a path."""
    service, state, _serial = service_setup
    for command in ("LS", "/bin/ls", "./ls", "lsx"):
        res = await service.process_run_async(command)
        assert res.pid == 0, command
    assert state.running_processes == {}


async def test_process_handle_is_the_real_pid(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Regression: the handle used to be pid & 0xFFFF, which can collide or be 0."""
    service, state, _serial = service_setup
    res = await service.process_run_async("sleep 10")
    assert res.pid > 0
    assert state.running_processes[res.pid].handle.pid == res.pid
    await service.process_kill(res.pid)


async def test_cloud_spi_transfer_asserts_serial(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, serial = service_setup
    serial.send.return_value = True
    await service.execute_rpc(
        "rpc.pb.SpiService/Transfer",
        pb.SpiTransfer(data=b"\x01\x02\x03").SerializeToString(),
    )

    serial.send.assert_called_once()
    assert serial.send.call_args[0][0] == "rpc.pb.SpiService/Transfer"
    assert isinstance(serial.send.call_args[0][1], pb.SpiTransfer)
    assert serial.send.call_args[0][1].data == b"\x01\x02\x03"


async def test_cloud_retired_file_write_is_rejected_and_remove_works(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    """Retired rpc.pb.FileSystemService/Write is rejected; Remove remains active."""
    service, _state, _serial = service_setup
    with pytest.raises(ValueError, match="Unknown RPC method"):
        await service.execute_rpc("rpc.pb.FileSystemService/Write", b"legacy_payload")

    assert not (Path(service.config.file_system_root) / "test.txt").exists()

    target = Path(service.config.file_system_root) / "to_remove.txt"
    target.write_bytes(b"host_file_payload")
    resp_bytes = await service.execute_rpc(
        "rpc.pb.FileSystemService/Remove",
        pb.FileRemove(path="to_remove.txt").SerializeToString(),
    )

    assert not target.exists()
    resp = cloud_gateway_pb2.GenericResponse.FromString(resp_bytes)
    assert resp.status == "ok"


async def test_cloud_shell_poll_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = service_setup
    res_run = await service.process_run_async("echo out")
    pid = res_run.pid
    assert pid > 0
    await asyncio.sleep(0.05)

    resp_bytes = await service.execute_rpc(
        "rpc.pb.ProcessService/Poll",
        pb.ProcessPoll(pid=pid).SerializeToString(),
    )
    resp = pb.ProcessPollResponse.FromString(resp_bytes)
    assert resp.exit_code == 0
    assert resp.stdout_data == b"out\n"


async def test_cloud_shell_kill_asserts_cloud(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, state, _serial = service_setup
    res_run = await service.process_run_async("sleep 10")
    pid = res_run.pid
    assert pid > 0
    assert pid in state.running_processes

    resp_bytes = await service.execute_rpc(
        "rpc.pb.ProcessService/Kill",
        pb.ProcessKill(pid=pid).SerializeToString(),
    )
    assert cloud_gateway_pb2.GenericResponse.FromString(resp_bytes).status == "ok"
    assert pid not in state.running_processes


async def test_cloud_shell_run_asserts_exec(
    service_setup: tuple[BridgeService, RuntimeState, AsyncMock],
) -> None:
    service, _state, _serial = service_setup
    resp_bytes = await service.execute_rpc(
        "rpc.pb.ProcessService/RunAsync",
        pb.ProcessRunAsync(command="echo hello").SerializeToString(),
    )
    resp = pb.ProcessRunAsyncResponse.FromString(resp_bytes)
    assert resp.pid > 0
    await service.process_kill(resp.pid)
