"""Unit tests for the OpenWrt UbusService component. [SIL-2]"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from dirty_equals import Contains, IsInstance, IsPartialDict
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.protocol import Command
from mcubridge.services.ubus import UbusService
from mcubridge.state.context import RuntimeState, create_runtime_state
from pytest_mock import MockerFixture


class MockRuntimeFacade:
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.handle_request = AsyncMock()
        self.process_run_async = AsyncMock(return_value=pb.ProcessRunAsyncResponse(pid=123))
        self.process_kill = AsyncMock(return_value=pb.GenericResponse(status="ok"))
        self.reset_link = AsyncMock(return_value=True)
        self.serial: AsyncMock | None = AsyncMock()
        self.serial.send = AsyncMock(return_value=True)
        self.mailbox_push = AsyncMock(return_value=True)
        self.mailbox_read = AsyncMock(return_value=b"")
        self.datastore_put = AsyncMock(return_value=True)
        self.datastore_get = AsyncMock(return_value=b"")
        self.file_write = AsyncMock(return_value=True)
        self.file_read = AsyncMock(return_value=b"")
        self.file_remove = AsyncMock(return_value=True)
        self.flush_console_queue = AsyncMock()
        self.console_queues: list[Any] = []
        self.ipc_requests: dict[bytes, asyncio.Queue[pb.CloudQueuedPublish]] = {}
        self.clock_sync: MagicMock | None = MagicMock()
        self.gpio: MagicMock | None = MagicMock()
        self.process_poll = AsyncMock(
            return_value=pb.ProcessPollResponse(
                status=0,
                exit_code=0,
                finished=True,
                stdout_data=b"hello stdout",
                stderr_data=b"",
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )


@pytest.fixture
def mock_runtime() -> MockRuntimeFacade:
    config = RuntimeConfig()
    state = create_runtime_state(config)
    state.connection_fsm.connect()
    state.connection_fsm.synchronize()
    state.mcu_version = (2, 8, 8)
    state.mcu_capabilities = pb.Capabilities(watchdog=True, spi=True, sd=True)
    return MockRuntimeFacade(config, state)


def test_ubus_service_init_and_properties(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    assert not service.is_active
    assert service.connection is None
    assert service.runtime == mock_runtime


def test_ubus_service_start_without_ubus_module(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    import mcubridge.services.ubus as ubus_mod

    mocker.patch.object(ubus_mod, "ubus", None)
    service = UbusService(mock_runtime)
    res = service.start()
    assert res is False
    assert not service.is_active


def test_ubus_service_start_with_mock_ubus_success(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_ubus.connect.return_value = True
    mock_ubus.BLOBMSG_TYPE_INT32 = 1
    mock_ubus.BLOBMSG_TYPE_STRING = 2

    mocker.patch.object(ubus_mod, "ubus", mock_ubus)
    service = UbusService(mock_runtime)
    res = service.start()

    assert res is True
    assert service.is_active
    assert service.connection is True
    assert mock_ubus.add.called
    call_args = mock_ubus.add.call_args[0]
    assert call_args[0] == "mcubridge"
    methods = call_args[1]
    assert methods.keys() == Contains(
        "status",
        "digital_write",
        "analog_write",
        "mailbox_push",
        "mailbox_read",
        "datastore_set",
        "datastore_get",
        "file_write",
        "process_run",
        "process_kill",
        "process_poll",
        "link_reset",
        "ping",
    )
    assert methods["digital_write"]["signature"] == {
        "pin": 1,
        "value": 1,
        "ubus_rpc_session": 2,
    }

    service.stop()
    assert not service.is_active
    mock_ubus.disconnect.assert_called_once_with()


def test_ubus_service_start_with_connection_error(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_ubus.connect.side_effect = OSError("Connection refused")

    mocker.patch.object(ubus_mod, "ubus", mock_ubus)
    service = UbusService(mock_runtime)
    res = service.start()

    assert res is False
    assert not service.is_active


def test_ubus_handle_status(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    status_resp = service.ubus_handle_status(MagicMock(), {})

    assert status_resp == IsPartialDict(
        connected=True,
        synchronized=True,
        version="2.8.8",
        capabilities=IsPartialDict(watchdog=True, spi=True, sd=True, i2c=False),
        metrics=IsInstance(dict),
        bridge=IsInstance(dict),
        system=IsInstance(dict),
        process_stats=IsInstance(list),
    )


def test_ubus_handle_status_with_dict_caps_and_no_version(mock_runtime: MockRuntimeFacade) -> None:
    mock_runtime.state.mcu_version = None
    mock_runtime.state.mcu_capabilities = {"i2c": True, "fpu": True}
    service = UbusService(mock_runtime)
    status_resp = service.ubus_handle_status(MagicMock(), {})

    assert status_resp["version"] == "unknown"
    assert status_resp["capabilities"]["i2c"] is True
    assert status_resp["capabilities"]["fpu"] is True

    mock_runtime.state.mcu_capabilities = None
    status_resp_none = service.ubus_handle_status(MagicMock(), {})
    assert status_resp_none["capabilities"] == {}


def test_ubus_register_methods_noop_when_conn_none(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    service.register_methods()
    assert not service.is_active


def test_ubus_schedule_async_without_running_loop(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    ran = [False]

    async def sample_coro() -> None:
        ran[0] = True

    service.schedule_async(sample_coro())
    assert ran[0] is True


async def test_ubus_run_pumps_native_loop_on_worker_thread(mock_runtime: MockRuntimeFacade) -> None:

    mock_ubus: Any = MagicMock()
    mock_ubus.connect.return_value = True
    service = UbusService(mock_runtime, ubus_module=mock_ubus)
    assert service.start()

    main_thread_id = threading.get_ident()
    worker_thread_ids: list[int] = []

    def finish_poll(timeout_ms: int) -> None:
        assert timeout_ms == 250
        worker_thread_ids.append(threading.get_ident())
        service.is_active = False

    mock_ubus.loop.side_effect = finish_poll
    await service.run()

    assert worker_thread_ids
    assert worker_thread_ids[0] != main_thread_id
    service.stop()


async def test_run_sync_bridges_worker_thread_to_running_loop(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime, loop=asyncio.get_running_loop())
    result = await asyncio.to_thread(service.run_sync, asyncio.sleep(0, result="completed"))
    assert result == "completed"


def test_ubus_stop_with_close_oserror(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_ubus.connect.return_value = True
    mock_ubus.disconnect.side_effect = OSError("Socket already closed")

    mocker.patch.object(ubus_mod, "ubus", mock_ubus)
    service = UbusService(mock_runtime)
    service.start()
    assert service.is_active

    service.stop()
    assert not service.is_active
    assert service.connection is None


async def test_ubus_handle_digital_write(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_digital_write(MagicMock(), {"pin": 13, "value": 1})

    assert res == {"status": "accepted", "pin": 13, "value": 1}
    await asyncio.sleep(0.01)
    assert mock_runtime.serial is not None
    mock_runtime.serial.send.assert_awaited_once_with(Command.CMD_DIGITAL_WRITE.value, pb.DigitalWrite(pin=13, value=1))


def test_ubus_handle_pin_write_serial_none(mock_runtime: MockRuntimeFacade) -> None:
    mock_runtime.serial = None
    service = UbusService(mock_runtime)
    res = service.ubus_handle_digital_write(None, {"pin": 13, "value": 1})
    assert res == {"status": "error", "message": "Serial service unavailable"}


async def test_ubus_handle_analog_write(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_analog_write(MagicMock(), {"pin": 9, "value": 128})

    assert res == {"status": "accepted", "pin": 9, "value": 128}
    await asyncio.sleep(0.01)
    assert mock_runtime.serial is not None
    mock_runtime.serial.send.assert_awaited_once_with(Command.CMD_ANALOG_WRITE.value, pb.AnalogWrite(pin=9, value=128))


async def test_ubus_handle_mailbox_push(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    message = "hola🌉"
    res = service.ubus_handle_mailbox_push(MagicMock(), {"message": message})

    assert res == {"status": "accepted", "message_length": len(message.encode("utf-8"))}
    await asyncio.sleep(0.01)
    mock_runtime.mailbox_push.assert_awaited_once_with(message.encode("utf-8"))


async def test_ubus_handle_datastore_set(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_datastore_set(MagicMock(), {"key": "temperature", "value": "24.5"})

    assert res == {"status": "accepted", "key": "temperature"}
    await asyncio.sleep(0.01)
    mock_runtime.datastore_put.assert_awaited_once_with("temperature", b"24.5")


def test_ubus_handle_process_run_sync(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_process_run(MagicMock(), {"command": "ls -l /tmp"})
    assert res == {"status": "ok", "pid": 123}
    assert mock_runtime.process_run_async.called


def test_ubus_handle_process_kill_sync(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_process_kill(MagicMock(), {"pid": 123})
    assert res == {"status": "ok", "pid": 123}
    assert mock_runtime.process_kill.called


def test_ubus_handle_process_kill_failure(mock_runtime: MockRuntimeFacade) -> None:
    mock_runtime.process_kill = AsyncMock(return_value=pb.GenericResponse(status="error"))
    service = UbusService(mock_runtime)
    res = service.ubus_handle_process_kill(MagicMock(), {"pid": 999})
    assert res == {"status": "error", "pid": 999}


def test_ubus_handle_process_poll_sync(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_process_poll(MagicMock(), {"pid": 123})
    assert res["status"] == "ok"
    assert res["exit_code"] == 0
    assert res["finished"] is True
    assert res["stdout"] == "hello stdout"
    assert res["stderr"] == ""


def test_ubus_handle_process_poll_binary_hex_fallback(mock_runtime: MockRuntimeFacade) -> None:
    mock_runtime.process_poll = AsyncMock(
        return_value=pb.ProcessPollResponse(
            status=1,
            exit_code=1,
            finished=True,
            stdout_data=b"\xff\xfe\xfd",
            stderr_data=b"\x80\x81",
            stdout_truncated=False,
            stderr_truncated=False,
        )
    )
    service = UbusService(mock_runtime)
    res = service.ubus_handle_process_poll(MagicMock(), {"pid": 456})
    assert res["status"] == "error"
    assert res["stdout"] == "<hex:fffefd>"
    assert res["stderr"] == "<hex:8081>"


def test_ubus_notify_lifecycle(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_ubus.connect.return_value = True

    mocker.patch.object(ubus_mod, "ubus", mock_ubus)
    service = UbusService(mock_runtime)

    # Inactive service returns False
    assert service.notify("sync", {"synchronized": True}) is False

    # Active service sends event
    service.start()
    assert service.notify("sync", {"synchronized": True}) is True
    assert mock_ubus.send.called
    assert mock_ubus.send.call_args[0][0] == "mcubridge.sync"

    # Error handling
    mock_ubus.send.side_effect = OSError("Send error")
    assert service.notify("sync", {"synchronized": True}) is False
    service.stop()


def test_ubus_notify_with_boolean_connection(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_ubus.connect.return_value = True

    mocker.patch.object(ubus_mod, "ubus", mock_ubus)
    service = UbusService(mock_runtime)

    service.start()
    assert service.notify("sync", {"synchronized": True}) is True
    assert mock_ubus.send.called
    assert mock_ubus.send.call_args[0][0] == "mcubridge.sync"
    assert mock_ubus.send.call_args[0][1] == {"synchronized": True}

    service.stop()
    assert mock_ubus.disconnect.called


def test_ubus_handle_datastore_get(mock_runtime: MockRuntimeFacade) -> None:
    mock_cache = AsyncMock()
    mock_cache.get.return_value = b"test_value"
    mock_runtime.state.datastore_cache = mock_cache

    service = UbusService(mock_runtime)
    res = service.ubus_handle_datastore_get(MagicMock(), {"key": "my_key"})

    assert res == {"status": "ok", "key": "my_key", "value": "test_value"}
    mock_cache.get.assert_awaited_with("my_key")


def test_ubus_handle_datastore_get_binary_and_not_found(mock_runtime: MockRuntimeFacade) -> None:
    mock_cache = AsyncMock()
    mock_cache.get.return_value = b"\xff\xfe\x01"
    mock_runtime.state.datastore_cache = mock_cache

    service = UbusService(mock_runtime)
    res = service.ubus_handle_datastore_get(MagicMock(), {"key": "binary_key"})
    assert res == {"status": "ok", "key": "binary_key", "value": "<hex:fffe01>"}

    # Not found
    mock_cache.get.return_value = None
    res_nf = service.ubus_handle_datastore_get(MagicMock(), {"key": "missing_key"})
    assert res_nf == {"status": "not_found", "key": "missing_key"}


def test_ubus_handle_mailbox_read(mock_runtime: MockRuntimeFacade) -> None:
    mock_queue = AsyncMock()
    mock_queue.popleft.return_value = b"hello from mcu"
    mock_runtime.state.mailbox_incoming_queue = mock_queue

    service = UbusService(mock_runtime)
    res = service.ubus_handle_mailbox_read(MagicMock(), {})

    assert res == {"status": "ok", "message": "hello from mcu"}
    mock_queue.popleft.assert_awaited()


def test_ubus_handle_mailbox_read_binary_and_empty(mock_runtime: MockRuntimeFacade) -> None:
    mock_queue = AsyncMock()
    mock_queue.popleft.return_value = b"\x80\x81\x82"
    mock_runtime.state.mailbox_incoming_queue = mock_queue

    service = UbusService(mock_runtime)
    res = service.ubus_handle_mailbox_read(MagicMock(), {})
    assert res == {"status": "ok", "message": "<hex:808182>"}

    # Empty queue
    mock_queue.popleft.side_effect = IndexError("popleft from empty deque")
    res_empty = service.ubus_handle_mailbox_read(MagicMock(), {})
    assert res_empty == {"status": "empty"}


def test_ubus_handle_datastore_get_cache_none(mock_runtime: MockRuntimeFacade) -> None:
    mock_runtime.state.datastore_cache = None
    service = UbusService(mock_runtime)
    res = service.ubus_handle_datastore_get(MagicMock(), {"key": "any"})
    assert res == {"status": "error", "message": "Datastore cache unavailable"}


async def test_ubus_run_sync_with_running_loop(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)

    async def sample_async() -> str:
        await asyncio.sleep(0.001)
        return "async_ok"

    res = await asyncio.to_thread(service.run_sync, sample_async())
    assert res == "async_ok"


async def test_ubus_handle_file_write(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    data = "café"
    res = service.ubus_handle_file_write(MagicMock(), {"path": "test.txt", "data": data})

    assert res == {"status": "accepted", "path": "test.txt", "bytes_requested": len(data.encode("utf-8"))}
    await asyncio.sleep(0.01)
    mock_runtime.file_write.assert_awaited_once_with("test.txt", data.encode("utf-8"))


def test_ubus_handle_link_reset(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_link_reset(MagicMock(), {})
    assert res == {"status": "ok"}
    mock_runtime.reset_link.assert_awaited_once()

    # Link reset failure path
    mock_runtime.reset_link = AsyncMock(return_value=False)
    res_err = service.ubus_handle_link_reset(MagicMock(), {})
    assert res_err == {"status": "error"}


def test_ubus_handle_ping(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_ping(MagicMock(), {})
    assert res["status"] == "ok"
    assert res["connected"] is True
    assert res["synchronized"] is True

    # Ping when not synchronized
    mock_runtime.state.connection_fsm.disconnect()
    res_unsync = service.ubus_handle_ping(MagicMock(), {})
    assert res_unsync["status"] == "not_synchronized"
    assert res_unsync["connected"] is False
    assert res_unsync["synchronized"] is False


async def test_ubus_service_run_loop(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    mock_ubus: Any = MagicMock()
    mock_ubus.loop = MagicMock()
    service = UbusService(mock_runtime, ubus_module=mock_ubus)
    # When not active, run returns immediately
    await service.run()
    mock_ubus.loop.assert_not_called()

    service.connection = True
    service.is_active = True

    async def cancel_soon() -> None:
        await asyncio.sleep(0.02)
        service.is_active = False

    async with asyncio.TaskGroup() as tg:
        tg.create_task(service.run())
        tg.create_task(cancel_soon())

    assert mock_ubus.loop.call_count > 0


def test_ubus_service_rpc_callback_execution(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_ubus.connect.return_value = True
    mocker.patch.object(ubus_mod, "ubus", mock_ubus)

    service = UbusService(mock_runtime)
    assert service.start() is True

    methods = mock_ubus.add.call_args[0][1]
    assert "ping" in methods
    cb = methods["ping"]["method"]

    # 1. Successful callback with reply
    mock_req = MagicMock()
    cb(mock_req, {})
    mock_req.reply.assert_called_once_with({"status": "ok", "connected": True, "synchronized": True})

    # 2. Callback when req is None or does not have reply
    cb(None, {})
    service.stop()

    # 3. Callback when handler raises an exception
    failing_service = UbusService(mock_runtime)
    mocker.patch.object(failing_service, "ubus_handle_ping", side_effect=ValueError("Test handler exception"))
    assert failing_service.start() is True
    fail_methods = mock_ubus.add.call_args[0][1]
    fail_cb = fail_methods["ping"]["method"]
    with pytest.raises(ValueError, match="Test handler exception"):
        fail_cb(mock_req, {})
    failing_service.stop()


def test_ubus_service_connect_returns_none(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_ubus.connect.return_value = None
    mocker.patch.object(ubus_mod, "ubus", mock_ubus)

    service = UbusService(mock_runtime)
    assert service.start(max_attempts=1) is False
    assert not service.is_active


def test_ubus_service_notify_branches(mock_runtime: MockRuntimeFacade) -> None:
    mock_backend: Any = MagicMock()
    service = UbusService(mock_runtime, ubus_module=mock_backend)
    # When inactive
    assert service.notify("test", {"a": 1}) is False

    service.connection = True
    service.is_active = True

    # Successful notification
    mock_backend.send.return_value = True
    assert service.notify("sync", {"status": "ok"}) is True
    mock_backend.send.assert_called_once_with("mcubridge.sync", {"status": "ok"})

    # Error during send
    mock_backend.send.side_effect = OSError("ubusd buffer full")
    assert service.notify("sync", {"status": "ok"}) is False

    # The module-level send result is the notification result.
    mock_backend.send.side_effect = None
    mock_backend.send.return_value = False
    assert service.notify("sync", {"status": "ok"}) is False

    service_no_send = UbusService(mock_runtime, ubus_module=MagicMock(spec=[]))
    service_no_send.connection = True
    service_no_send.is_active = True
    assert service_no_send.notify("sync", {"status": "ok"}) is False


def test_ubus_service_register_methods_requires_module_api(mock_runtime: MockRuntimeFacade) -> None:
    mock_ubus: Any = MagicMock(spec=[])
    service = UbusService(mock_runtime, ubus_module=mock_ubus)
    service.connection = True
    with pytest.raises(RuntimeError, match="callable add"):
        service.register_methods()


def test_ubus_service_stop_disconnects_module_api(mock_runtime: MockRuntimeFacade) -> None:
    mock_backend: Any = MagicMock()
    service = UbusService(mock_runtime, ubus_module=mock_backend)
    service.connection = True
    service.is_active = True
    event_loop = asyncio.new_event_loop()
    service.loop = event_loop

    service.stop()

    assert not service.is_active
    assert vars(service)["connection"] is None
    assert service.loop is None
    mock_backend.disconnect.assert_called_once_with()
    event_loop.close()


def test_ubus_service_start_rejects_non_boolean_connect(mock_runtime: MockRuntimeFacade) -> None:
    mock_backend: Any = MagicMock()
    mock_backend.connect.return_value = 1
    service = UbusService(mock_runtime, ubus_module=mock_backend)

    assert service.start(max_attempts=1) is False
    assert service.connection is None
    assert not service.is_active


def test_ubus_service_start_disconnects_after_registration_failure(mock_runtime: MockRuntimeFacade) -> None:
    mock_backend: Any = MagicMock()
    mock_backend.connect.return_value = True
    mock_backend.add.side_effect = OSError("registration failed")
    service = UbusService(mock_runtime, ubus_module=mock_backend)

    assert service.start(max_attempts=1) is False
    mock_backend.disconnect.assert_called_once_with()
    assert service.connection is None
    assert not service.is_active


def test_ubus_handle_clock_status_and_sync(mock_runtime: MockRuntimeFacade) -> None:

    service = UbusService(mock_runtime)

    # 1. With clock service available
    mock_clock = MagicMock()
    mock_clock.get_status.return_value = {"status": "ok", "offset_us": 120}
    mock_clock.sync_now = AsyncMock(return_value={"status": "ok", "synced": True})
    mock_runtime.clock_sync = mock_clock

    res_status = service.ubus_handle_clock_status(MagicMock(), {})
    assert res_status == {"status": "ok", "offset_us": 120}

    res_sync = service.ubus_handle_clock_sync(MagicMock(), {})
    assert res_sync == {"status": "ok", "synced": True}

    # 2. Without clock service
    mock_runtime.clock_sync = None
    err_status = service.ubus_handle_clock_status(MagicMock(), {})
    assert err_status["status"] == "error"
    err_sync = service.ubus_handle_clock_sync(MagicMock(), {})
    assert err_sync["status"] == "error"


def test_ubus_handle_pin_subscribe(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)

    # 1. With GPIO service available
    mock_gpio = MagicMock()
    mock_gpio.subscribe_pin = AsyncMock(return_value={"status": "ok", "pin": 13})
    mock_runtime.gpio = mock_gpio

    res = service.ubus_handle_pin_subscribe(MagicMock(), {"pin": 13, "mode": "INPUT", "interval_ms": 100})
    assert res == {"status": "ok", "pin": 13}
    mock_gpio.subscribe_pin.assert_awaited_once_with(13, "INPUT", 100, 1, True)

    # 2. Without GPIO service
    mock_runtime.gpio = None
    err = service.ubus_handle_pin_subscribe(MagicMock(), {"pin": 13})
    assert err["status"] == "error"


async def test_ubus_schedule_async_in_running_loop(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    executed: list[bool] = []

    async def _sample_coro() -> None:
        executed.append(True)

    service.schedule_async(_sample_coro())
    await asyncio.sleep(0.01)
    assert executed == [True]


def test_ubus_schedule_async_with_target_loop(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    service = UbusService(mock_runtime)
    mock_loop = MagicMock()
    mock_loop.is_running.return_value = True
    service.loop = mock_loop

    mock_run_ts = MagicMock()
    mocker.patch.object(asyncio, "run_coroutine_threadsafe", mock_run_ts)

    async def _sample_coro() -> None:
        pass

    coro = _sample_coro()
    try:
        service.schedule_async(coro)
        mock_run_ts.assert_called_once_with(coro, mock_loop)
    finally:
        coro.close()


def test_ubus_run_coro_sync_threadsafe(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    service = UbusService(mock_runtime)
    mock_loop = MagicMock()
    mock_loop.is_running.return_value = True
    service.loop = mock_loop

    mock_fut = MagicMock()
    mock_fut.result.return_value = {"status": "ok"}
    mock_run_ts = mocker.patch.object(asyncio, "run_coroutine_threadsafe", return_value=mock_fut)

    async def _sample() -> dict[str, str]:
        return {"status": "ok"}

    coro = _sample()
    try:
        res = service.run_sync(coro)
        assert res == {"status": "ok"}
        mock_run_ts.assert_called_once_with(coro, mock_loop)
    finally:
        coro.close()


async def test_ubus_run_coro_sync_same_loop(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime, loop=asyncio.get_running_loop())

    async def _sample() -> dict[str, str]:
        return {"status": "same_loop"}

    with pytest.raises(RuntimeError, match="cannot block the asyncio loop"):
        service.run_sync(_sample())


def test_ubus_register_methods_no_add(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime, ubus_module=object())
    service.connection = True
    with pytest.raises(RuntimeError, match="callable add"):
        service.register_methods()


@given(raw=st.binary())
def test_format_ubus_bytes_property(raw: bytes) -> None:
    from mcubridge.services.ubus import format_ubus_bytes

    formatted = format_ubus_bytes(raw)
    assert isinstance(formatted, str)
    try:
        decoded = raw.decode("utf-8")
        assert formatted == decoded
    except UnicodeDecodeError:
        assert formatted == f"<hex:{raw.hex()}>"


@given(pin=st.integers(0, 255), val=st.integers(0, 1023))
def test_ubus_handle_pin_write_property(pin: int, val: int) -> None:
    cfg = RuntimeConfig()
    state = create_runtime_state(cfg)
    mock_rt = MockRuntimeFacade(cfg, state)
    svc = UbusService(mock_rt)
    try:
        # Digital write
        resp_d = svc.ubus_handle_digital_write(None, {"pin": pin, "value": val})
        assert resp_d == {"status": "accepted", "pin": pin, "value": val}
        assert mock_rt.serial is not None
        mock_rt.serial.send.assert_awaited_once_with(
            Command.CMD_DIGITAL_WRITE.value,
            pb.DigitalWrite(pin=pin, value=val),
        )

        # Analog write
        resp_a = svc.ubus_handle_analog_write(None, {"pin": pin, "value": val})
        assert resp_a == {"status": "accepted", "pin": pin, "value": val}
        assert mock_rt.serial is not None
        mock_rt.serial.send.assert_awaited_with(
            Command.CMD_ANALOG_WRITE.value,
            pb.AnalogWrite(pin=pin, value=val),
        )
    finally:
        state.cleanup()


@given(
    key=st.text(min_size=1, max_size=64).filter(lambda k: "\x00" not in k),
    val=st.text(max_size=128).filter(lambda v: "\x00" not in v),
    msg_text=st.text(max_size=128),
)
def test_ubus_handle_datastore_and_mailbox_property(key: str, val: str, msg_text: str) -> None:
    cfg = RuntimeConfig()
    state = create_runtime_state(cfg)
    mock_rt = MockRuntimeFacade(cfg, state)
    svc = UbusService(mock_rt)
    try:
        # Datastore set
        resp_ds = svc.ubus_handle_datastore_set(None, {"key": key, "value": val})
        assert resp_ds == {"status": "accepted", "key": key}
        mock_rt.datastore_put.assert_awaited_once_with(key, val.encode("utf-8"))

        # Mailbox push
        resp_mb = svc.ubus_handle_mailbox_push(None, {"message": msg_text})
        assert resp_mb == {"status": "accepted", "message_length": len(msg_text.encode("utf-8"))}
        mock_rt.mailbox_push.assert_awaited_once_with(msg_text.encode("utf-8"))
    finally:
        state.cleanup()


async def test_ubus_service_run_rejects_non_callable_loop(mock_runtime: MockRuntimeFacade) -> None:
    """Reject a connected backend that does not expose an invokable event loop."""
    mock_ubus: Any = MagicMock(spec=[])
    service = UbusService(mock_runtime, ubus_module=mock_ubus)
    service.connection = True

    with pytest.raises(RuntimeError, match=r"no callable loop\(\) API"):
        await service.run()


def test_ubus_detect_fd_proc_missing(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    service = UbusService(mock_runtime)
    mocker.patch.object(Path, "exists", return_value=False)
    assert service.detect_ubus_fd() is None


def test_ubus_detect_fd_success_and_failures(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    service = UbusService(mock_runtime)
    mocker.patch.object(Path, "exists", return_value=True)

    entry_non_int = MagicMock()
    entry_non_int.name = "non_int"

    entry_pipe = MagicMock()
    entry_pipe.name = "10"
    entry_pipe.readlink.return_value = "pipe:[123]"

    entry_oserr = MagicMock()
    entry_oserr.name = "11"
    entry_oserr.readlink.side_effect = OSError("Access denied")

    entry_socket = MagicMock()
    entry_socket.name = "12"
    entry_socket.readlink.return_value = "socket:[456]"

    mocker.patch.object(
        Path,
        "iterdir",
        return_value=[entry_non_int, entry_pipe, entry_oserr, entry_socket],
    )

    mock_socket_instance = MagicMock()
    mock_socket_instance.getpeername.return_value = "/var/run/ubus/ubus.sock"
    mocker.patch("socket.socket", return_value=mock_socket_instance)

    assert service.detect_ubus_fd() == 12
    mock_socket_instance.detach.assert_called_once()


def test_ubus_pump_events_with_detected_socket_fd(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    mock_ubus = MagicMock()
    service = UbusService(mock_runtime, ubus_module=mock_ubus)
    service.is_active = True

    mocker.patch.object(service, "detect_ubus_fd", return_value=42)

    calls = 0

    def mock_select(
        rlist: list[int], _w: list[int], _x: list[int], _to: float
    ) -> tuple[list[int], list[int], list[int]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ([42], [], [])
        service.is_active = False
        return ([], [], [])

    mocker.patch("select.select", side_effect=mock_select)

    service.pump_events(mock_ubus.loop)
    mock_ubus.loop.assert_called_once_with(0)


def test_ubus_pump_events_fallback_when_fd_not_found(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    mock_ubus = MagicMock()
    service = UbusService(mock_runtime, ubus_module=mock_ubus)
    service.is_active = True

    mocker.patch.object(service, "detect_ubus_fd", return_value=None)

    def mock_loop(timeout: int) -> None:
        service.is_active = False

    mock_ubus.loop.side_effect = mock_loop

    service.pump_events(mock_ubus.loop)
    mock_ubus.loop.assert_called_once_with(250)


def test_ubus_connect_preload_uloop_init_handles_error(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    mock_ubus = MagicMock()
    mock_ubus.connect.return_value = True
    service = UbusService(mock_runtime, ubus_module=mock_ubus)

    mocker.patch("ctypes.util.find_library", return_value="libubox.so")
    mocker.patch("ctypes.CDLL", side_effect=OSError("Failed to load libubox"))

    assert service.start(max_attempts=1) is True


def test_ubus_detect_fd_socket_peer_oserror_and_iterdir_oserror(
    mock_runtime: MockRuntimeFacade, mocker: MockerFixture
) -> None:
    service = UbusService(mock_runtime)
    mocker.patch.object(Path, "exists", return_value=True)

    entry_sock = MagicMock()
    entry_sock.name = "15"
    entry_sock.readlink.return_value = "socket:[999]"

    mocker.patch.object(Path, "iterdir", return_value=[entry_sock])

    mock_s = MagicMock()
    mock_s.getpeername.side_effect = OSError("Socket not connected")
    mocker.patch("socket.socket", return_value=mock_s)

    assert service.detect_ubus_fd() is None
    mock_s.detach.assert_called_once()

    mocker.patch.object(Path, "iterdir", side_effect=OSError("Access denied to proc fd"))
    assert service.detect_ubus_fd() is None


def test_ubus_pump_events_select_error_and_loop_error(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    mock_ubus = MagicMock()
    service = UbusService(mock_runtime, ubus_module=mock_ubus)
    service.is_active = True

    mocker.patch.object(service, "detect_ubus_fd", return_value=50)
    mocker.patch("select.select", side_effect=ValueError("Bad file descriptor"))

    service.pump_events(mock_ubus.loop)
    mock_ubus.loop.assert_not_called()

    # Test loop_ubus(0) error handling
    service.is_active = True
    calls = 0

    def mock_select(r: list[int], w: list[int], x: list[int], to: float) -> tuple[list[int], list[int], list[int]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ([50], [], [])
        service.is_active = False
        return ([], [], [])

    mocker.patch("select.select", side_effect=mock_select)
    mock_ubus.loop.side_effect = RuntimeError("Dispatch error")

    service.pump_events(mock_ubus.loop)
    mock_ubus.loop.assert_called_once_with(0)


def test_ubus_pump_events_fallback_loop_error(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    mock_ubus = MagicMock()
    service = UbusService(mock_runtime, ubus_module=mock_ubus)
    service.is_active = True

    mocker.patch.object(service, "detect_ubus_fd", return_value=None)

    def mock_loop(to: int) -> None:
        service.is_active = False
        raise RuntimeError("Native poll error")

    mock_ubus.loop.side_effect = mock_loop

    service.pump_events(mock_ubus.loop)
    mock_ubus.loop.assert_called_once_with(250)


async def test_ubus_service_run_cancelled_stops_service(mock_runtime: MockRuntimeFacade, mocker: MockerFixture) -> None:
    import time

    mock_ubus = MagicMock()
    service = UbusService(mock_runtime, ubus_module=mock_ubus)
    service.connection = True
    service.is_active = True

    def slow_pump(_loop: Any) -> None:
        while service.is_active:
            time.sleep(0.01)

    mocker.patch.object(service, "pump_events", side_effect=slow_pump)

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert service.is_active is False
