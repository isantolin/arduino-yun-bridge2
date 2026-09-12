"""Unit tests for ClockSyncService and GpioService."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock

import pytest

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    spool_dir = f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    return RuntimeConfig(
        allowed_commands=("echo", "ls"),
        serial_shared_secret=b"testshared",
        file_system_root=fs_root,
        cloud_spool_dir=spool_dir,
        allow_non_tmp_paths=True,
    )


@pytest.mark.asyncio
async def test_clock_sync_service() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(config, state, mock_serial)

    try:
        clock = service.clock_sync
        assert clock.get_status()["sync_count"] == 0

        # When not connected, sync_now returns immediately
        status = await clock.sync_now()
        assert status["sync_count"] == 0

        # Simulate connected state
        state.connection_fsm.connect()

        # Simulate MCU response
        now_us = time.time_ns() // 1000
        mock_serial.send.return_value = pb.ClockSyncResponse(
            host_time_us=now_us - 1000,
            mcu_time_us=500000,
        )

        res = await clock.sync_now()
        assert res["sync_count"] == 1
        assert res["rtt_us"] >= 0
        assert res["is_synchronized"] is True
        assert state.clock_sync_count == 1

        # Test MCU response handler via dispatch registry
        handler = service.mcu_registry[pb.Command.CMD_CLOCK_SYNC_RESP]
        await handler(
            1,
            pb.ClockSyncResponse(
                host_time_us=now_us - 2000,
                mcu_time_us=400000,
            ),
        )
        assert state.clock_sync_count == 2
    finally:
        service.cleanup()


@pytest.mark.asyncio
async def test_gpio_service() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(config, state, mock_serial)

    try:
        gpio = service.gpio
        state.connection_fsm.connect()

        mock_serial.send.return_value = pb.PinSubscribeResponse(pin=13, success=True)

        res = await gpio.subscribe_pin(13, mode="INPUT_PULLUP", interval_ms=100, hysteresis=2)
        assert res["status"] == "ok"
        assert res["pin"] == 13
        assert res["enabled"] is True
        assert 13 in state.pin_subscriptions
        assert state.pin_subscriptions[13]["mode"] == "INPUT_PULLUP"

        # Receive streaming pin event via dispatch registry
        evt = pb.PinUpdateEvent(pin=13, value=1, timestamp_micros=999999)
        evt_handler = service.mcu_registry[pb.Command.CMD_PIN_UPDATE_EVENT]
        await evt_handler(0, evt)
        assert state.pin_events_count == 1

        cloud_msg = state.cloud_publish_queue.get_nowait()
        assert cloud_msg.topic_name == "gpio/pin_13/update"
        assert cloud_msg.payload == b"1"

        # Disable subscription
        res_dis = await gpio.subscribe_pin(13, enabled=False)
        assert res_dis["status"] == "ok"
        assert 13 not in state.pin_subscriptions
    finally:
        service.cleanup()


@pytest.mark.asyncio
async def test_ubus_clock_and_gpio_handlers() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(config, state, mock_serial)

    try:
        ubus = service.ubus_service
        state.connection_fsm.connect()

        mock_serial.send.return_value = pb.PinSubscribeResponse(pin=7, success=True)

        sub_reply = ubus.ubus_handle_pin_subscribe(
            None,
            {"pin": 7, "mode": "INPUT", "interval_ms": 50, "hysteresis": 1, "enabled": 1},
        )
        assert sub_reply["status"] == "ok"
        assert sub_reply["pin"] == 7

        clock_reply = ubus.ubus_handle_clock_status(None, {})
        assert "sync_count" in clock_reply

        status_reply = ubus.ubus_handle_status(None, {})
        assert "clock_status" in status_reply
        assert "pin_subscriptions" in status_reply
    finally:
        service.cleanup()


@pytest.mark.asyncio
async def test_clock_sync_skipped_when_capability_missing() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(config, state, mock_serial)

    try:
        clock = service.clock_sync
        state.connection_fsm.connect()

        # 1. Capabilities dict with clock_sync explicitly False -> skipped without sending
        state.mcu_capabilities = {"watchdog": True, "clock_sync": False}
        status = await clock.sync_now()
        assert status["sync_count"] == 0
        assert mock_serial.send.call_count == 0
        assert clock.fsm.current_state_value == "unsupported"

        # 2. Capabilities dict with clock_sync enabled -> executes send with timeout=1.0
        state.mcu_capabilities = {"watchdog": True, "clock_sync": True}
        now_us = time.time_ns() // 1000
        mock_serial.send.return_value = pb.ClockSyncResponse(
            host_time_us=now_us - 500,
            mcu_time_us=123456,
        )
        res = await clock.sync_now()
        assert res["sync_count"] == 1
        assert res["state"] == "synchronized"
        assert res["is_synchronized"] is True
        assert mock_serial.send.call_count == 1
        call_kwargs = mock_serial.send.call_args.kwargs
        assert call_kwargs.get("timeout") == 1.0

        # 3. Subsequent probe failure while synchronized transitions to degraded
        mock_serial.send.return_value = False
        res_fail = await clock.sync_now()
        assert res_fail["state"] == "degraded"
        assert res_fail["is_synchronized"] is False

        # 4. First-time probe failure on board without clock sync transitions to unsupported
        clock.fsm.disconnect()
        assert clock.fsm.current_state_value == "idle"
        state.mcu_capabilities = pb.Capabilities(watchdog=True, spi=True)
        mock_serial.send.return_value = None
        res_probe_fail = await clock.sync_now()
        assert res_probe_fail["state"] == "unsupported"
        assert res_probe_fail["is_synchronized"] is False
    finally:
        service.cleanup()


@pytest.mark.asyncio
async def test_clock_sync_fsm_lifecycle() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    mock_serial = AsyncMock(spec=SerialTransport)
    service = BridgeService(config, state, mock_serial)

    try:
        clock = service.clock_sync
        assert clock.fsm.current_state_value == "idle"

        # Disconnected sync keeps idle
        await clock.sync_now()
        assert clock.fsm.current_state_value == "idle"

        state.connection_fsm.connect()

        # Capability missing -> unsupported
        state.mcu_capabilities = {"clock_sync": False}
        await clock.sync_now()
        assert clock.fsm.current_state_value == "unsupported"

        # Reconnect / recheck
        clock.fsm.recheck_capability()
        assert clock.fsm.current_state_value == "idle"

        # Successful sync -> synchronized
        state.mcu_capabilities = {"clock_sync": True}
        now_us = time.time_ns() // 1000
        mock_serial.send.return_value = pb.ClockSyncResponse(host_time_us=now_us - 100, mcu_time_us=1000)
        await clock.sync_now()
        assert clock.fsm.current_state_value == "synchronized"

        # Failure while synchronized -> degraded
        mock_serial.send.return_value = None
        await clock.sync_now()
        assert clock.fsm.current_state_value == "degraded"

        # Disconnect -> idle
        clock.fsm.disconnect()
        assert clock.fsm.current_state_value == "idle"
    finally:
        service.cleanup()

