"""Unit tests for the OpenWrt UbusService component. [SIL-2]"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.ubus import UbusService
from mcubridge.state.context import RuntimeState, create_runtime_state


class MockRuntimeFacade:
    def __init__(self, config: RuntimeConfig, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self.handle_request = AsyncMock()
        self.run_process = AsyncMock(return_value=123)
        self.kill_process = AsyncMock(return_value=(True, None))
        self.poll_process = AsyncMock(
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
    state.mark_transport_connected()
    state.mark_synchronized()
    state.mcu_version = (2, 8, 6)
    state.mcu_capabilities = pb.Capabilities(watchdog=True, spi=True, sd=True)
    return MockRuntimeFacade(config, state)


def test_ubus_service_init_and_properties(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    assert not service.is_active
    assert service.connection is None
    assert service.runtime == mock_runtime


def test_ubus_service_start_without_ubus_module(
    mock_runtime: MockRuntimeFacade, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcubridge.services.ubus as ubus_mod

    monkeypatch.setattr(ubus_mod, "ubus", None)
    service = UbusService(mock_runtime)
    res = service.start()
    assert res is False
    assert not service.is_active


def test_ubus_service_start_with_mock_ubus_success(
    mock_runtime: MockRuntimeFacade, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    mock_ubus.connect.return_value = mock_conn
    mock_ubus.INT32 = 1
    mock_ubus.STRING = 2

    monkeypatch.setattr(ubus_mod, "ubus", mock_ubus)
    service = UbusService(mock_runtime)
    res = service.start()

    assert res is True
    assert service.is_active
    assert service.connection == mock_conn
    assert mock_conn.add.called
    call_args = mock_conn.add.call_args[0]
    assert call_args[0] == "mcubridge"
    methods = call_args[1]
    assert "status" in methods
    assert "digital_write" in methods
    assert "analog_write" in methods
    assert "mailbox_push" in methods
    assert "datastore_set" in methods
    assert "process_run" in methods
    assert "process_kill" in methods
    assert "process_poll" in methods

    service.stop()
    assert not service.is_active
    assert mock_conn.close.called


def test_ubus_service_start_with_connection_error(
    mock_runtime: MockRuntimeFacade, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_ubus.connect.side_effect = OSError("Connection refused")

    monkeypatch.setattr(ubus_mod, "ubus", mock_ubus)
    service = UbusService(mock_runtime)
    res = service.start()

    assert res is False
    assert not service.is_active


def test_ubus_handle_status(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    status_resp = service.ubus_handle_status(MagicMock(), {})

    assert status_resp["connected"] is True
    assert status_resp["synchronized"] is True
    assert status_resp["version"] == "2.8.6"
    assert status_resp["capabilities"]["watchdog"] is True
    assert status_resp["capabilities"]["spi"] is True
    assert status_resp["capabilities"]["sd"] is True
    assert status_resp["capabilities"]["i2c"] is False


def test_ubus_handle_status_with_dict_caps_and_no_version(mock_runtime: MockRuntimeFacade) -> None:
    mock_runtime.state.mcu_version = None
    mock_runtime.state.mcu_capabilities = {"i2c": True, "fpu": True}
    service = UbusService(mock_runtime)
    status_resp = service.ubus_handle_status(MagicMock(), {})

    assert status_resp["version"] == "unknown"
    assert status_resp["capabilities"]["i2c"] is True
    assert status_resp["capabilities"]["fpu"] is True


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


def test_ubus_stop_with_close_oserror(mock_runtime: MockRuntimeFacade, monkeypatch: pytest.MonkeyPatch) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    mock_ubus.connect.return_value = mock_conn
    mock_conn.close.side_effect = OSError("Socket already closed")

    monkeypatch.setattr(ubus_mod, "ubus", mock_ubus)
    service = UbusService(mock_runtime)
    service.start()
    assert service.is_active

    service.stop()
    assert not service.is_active
    assert service.connection is None


@pytest.mark.asyncio
async def test_ubus_handle_digital_write(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_digital_write(MagicMock(), {"pin": 13, "value": 1})

    assert res == {"status": "ok", "pin": 13, "value": 1}
    await asyncio.sleep(0.01)
    assert mock_runtime.handle_request.called
    inbound = mock_runtime.handle_request.call_args[0][0]
    assert "digital/13/set" in inbound.topic_name
    assert inbound.payload == b"1"


@pytest.mark.asyncio
async def test_ubus_handle_analog_write(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_analog_write(MagicMock(), {"pin": 9, "value": 128})

    assert res == {"status": "ok", "pin": 9, "value": 128}
    await asyncio.sleep(0.01)
    assert mock_runtime.handle_request.called
    inbound = mock_runtime.handle_request.call_args[0][0]
    assert "analog/9/set" in inbound.topic_name
    assert inbound.payload == b"128"


@pytest.mark.asyncio
async def test_ubus_handle_mailbox_push(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_mailbox_push(MagicMock(), {"message": "hello_mcu"})

    assert res == {"status": "ok", "message_length": 9}
    await asyncio.sleep(0.01)
    assert mock_runtime.handle_request.called
    inbound = mock_runtime.handle_request.call_args[0][0]
    assert "mailbox/push" in inbound.topic_name
    assert inbound.payload == b"hello_mcu"


@pytest.mark.asyncio
async def test_ubus_handle_datastore_set(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_datastore_set(MagicMock(), {"key": "temperature", "value": "24.5"})

    assert res == {"status": "ok", "key": "temperature"}
    await asyncio.sleep(0.01)
    assert mock_runtime.handle_request.called
    inbound = mock_runtime.handle_request.call_args[0][0]
    assert "datastore/temperature/set" in inbound.topic_name
    assert inbound.payload == b"24.5"


def test_ubus_handle_process_run_sync(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_process_run(MagicMock(), {"command": "ls -l /tmp"})
    assert res == {"status": "ok", "pid": 123}
    assert mock_runtime.run_process.called


def test_ubus_handle_process_kill_sync(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_process_kill(MagicMock(), {"pid": 123})
    assert res == {"status": "ok", "pid": 123, "error": ""}
    assert mock_runtime.kill_process.called


def test_ubus_handle_process_kill_failure(mock_runtime: MockRuntimeFacade) -> None:
    mock_runtime.kill_process = AsyncMock(return_value=(False, "PID not found"))
    service = UbusService(mock_runtime)
    res = service.ubus_handle_process_kill(MagicMock(), {"pid": 999})
    assert res == {"status": "error", "pid": 999, "error": "PID not found"}


def test_ubus_handle_process_poll_sync(mock_runtime: MockRuntimeFacade) -> None:
    service = UbusService(mock_runtime)
    res = service.ubus_handle_process_poll(MagicMock(), {"pid": 123})
    assert res["status"] == "ok"
    assert res["exit_code"] == 0
    assert res["finished"] is True
    assert res["stdout"] == "hello stdout"
    assert res["stderr"] == ""


def test_ubus_handle_process_poll_binary_hex_fallback(mock_runtime: MockRuntimeFacade) -> None:
    mock_runtime.poll_process = AsyncMock(
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


def test_ubus_notify_lifecycle(mock_runtime: MockRuntimeFacade, monkeypatch: pytest.MonkeyPatch) -> None:
    import mcubridge.services.ubus as ubus_mod

    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    mock_ubus.connect.return_value = mock_conn

    monkeypatch.setattr(ubus_mod, "ubus", mock_ubus)
    service = UbusService(mock_runtime)

    # Inactive service returns False
    assert service.notify("sync", {"synchronized": True}) is False

    # Active service sends event
    service.start()
    assert service.notify("sync", {"synchronized": True}) is True
    assert mock_conn.send.called
    assert mock_conn.send.call_args[0][0] == "mcubridge.sync"

    # Error handling
    mock_conn.send.side_effect = OSError("Send error")
    assert service.notify("sync", {"synchronized": True}) is False
