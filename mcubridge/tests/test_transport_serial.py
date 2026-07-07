from unittest.mock import AsyncMock, MagicMock
from typing import Any

import asyncio
import pytest
from cobs import cobs
from mcubridge.protocol import protocol
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.frame import build_frame
from mcubridge.protocol.protocol import Command
from mcubridge.services.runtime import BridgeService
from mcubridge.state.context import create_runtime_state
from mcubridge.transport.serial import SerialTransport


def _make_config() -> RuntimeConfig:
    import os
    import time

    fs_root = f".tmp_tests/mcubridge-test-fs-{os.getpid()}-{time.time_ns()}"
    f".tmp_tests/mcubridge-test-spool-{os.getpid()}-{time.time_ns()}"
    return RuntimeConfig(
        serial_port="/dev/ttyATH0",
        topic_prefix="br",
        allowed_commands=("*",),
        serial_shared_secret=b"secret123",
        file_system_root=fs_root,
        cloud_spool_dir="",
        allow_non_tmp_paths=True,
    )


@pytest.mark.asyncio
async def test_process_packet_crc_mismatch_reports_crc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        state.mark_transport_connected()
        state.mark_synchronized()
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        transport = SerialTransport(config, state, service)

        # Create an invalid frame manually (e.g. version mismatch to trigger ValueError in parse_frame)
        raw = b"\xff" + b"x" * 20

        def mock_decode(data: Any) -> bytes:
            return raw

        monkeypatch.setattr(cobs, "decode", mock_decode)

        # Manual call to async method
        await getattr(transport, "_process_packet")(b"\x02encoded")

        assert state.serial_decode_errors == 1
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_success_dispatches() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))

        service.handle_mcu_frame = AsyncMock()

        frame_bytes = build_frame(command_id=Command.CMD_CONSOLE_WRITE.value, sequence_id=0, payload=b"hi")
        encoded = cobs.encode(frame_bytes)
        transport = SerialTransport(config, state, service)
        await getattr(transport, "_process_packet")(encoded)

        service.handle_mcu_frame.assert_awaited_once_with(Command.CMD_CONSOLE_WRITE.value, 0, b"hi")
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_negotiation_ack_switches_local_baudrate() -> None:
    config = _make_config()
    config.serial_baud = 230400
    config.serial_safe_baud = 115200
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))

        transport = SerialTransport(config, state, service)

        mock_serial = AsyncMock()
        mock_serial.transport = AsyncMock()
        from serialx import Serial

        mock_serial.transport.serial = MagicMock(spec=Serial)
        mock_serial.transport.serial.baudrate = config.serial_safe_baud

        transport.serial = mock_serial

        setattr(transport, "_negotiating", True)
        setattr(transport, "_negotiation_future", asyncio.get_running_loop().create_future())

        encoded = cobs.encode(
            build_frame(
                command_id=Command.CMD_SET_BAUDRATE_RESP.value,
                sequence_id=0,
                payload=b"",
            )
        )
        await getattr(transport, "_process_packet")(encoded)

        assert await getattr(transport, "_negotiation_future")
        assert mock_serial.transport.serial.baudrate == config.serial_baud
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_write_frame_debug_logs_unknown_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))
        import mcubridge.transport.serial

        transport = SerialTransport(config, state, service)
        mock_serial = AsyncMock()
        mock_serial.is_open = True
        transport.serial = mock_serial

        def mock_is_enabled(lvl: int) -> bool:
            return True

        monkeypatch.setattr(
            mcubridge.transport.serial.logger,
            "is_enabled_for",
            mock_is_enabled,
        )
        seen: dict[str, str] = {}

        def mock_debug(msg: str, *args: Any) -> Any:
            return seen.setdefault("msg", msg % args)

        monkeypatch.setattr(
            mcubridge.transport.serial.logger,
            "debug",
            mock_debug,
        )

        def mock_log(lvl: int, msg: str, *args: Any) -> Any:
            return seen.setdefault("msg", msg % args)

        monkeypatch.setattr(
            mcubridge.transport.serial.logger,
            "log",
            mock_log,
        )

        ok = await transport.send(0xFE, b"payload")
        assert ok
        assert mock_serial.write.called
        # Check that the command 0xFE is present in the encoded hex string
        assert "fe" in seen.get("msg", "").lower()
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_write_frame_returns_false_on_write_error() -> None:
    config = _make_config()
    state = create_runtime_state(config)
    try:
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))

        transport = SerialTransport(config, state, service)
        mock_serial = AsyncMock()
        mock_serial.is_open = True
        mock_serial.write.side_effect = OSError("boom")
        transport.serial = mock_serial

        ok = await transport.send(Command.CMD_CONSOLE_WRITE.value, b"hi")
        assert not ok
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_process_packet_fallback_triggers_negotiation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config()
    config.serial_baud = protocol.DEFAULT_BAUDRATE
    config.serial_safe_baud = 57600
    config.serial_fallback_threshold = 2
    state = create_runtime_state(config)
    try:
        state.mark_transport_connected()
        state.mark_synchronized()
        service = BridgeService(config, state, AsyncMock(spec=SerialTransport))

        transport = SerialTransport(config, state, service)

        # Mock negotiation method
        setattr(transport, "_negotiate_baudrate", AsyncMock(return_value=True))

        # Create an invalid frame manually
        raw = b"\xff" + b"x" * 20

        def mock_decode_fallback(data: Any) -> bytes:
            return raw

        monkeypatch.setattr(cobs, "decode", mock_decode_fallback)

        await getattr(transport, "_process_packet")(b"\x02encoded")
        assert getattr(transport, "_consecutive_crc_errors") == 1

        getattr(transport, "_negotiate_baudrate").assert_not_called()

        # Second error (threshold reached)
        await getattr(transport, "_process_packet")(b"\x02encoded")
        assert getattr(transport, "_consecutive_crc_errors") == 0

        getattr(transport, "_negotiate_baudrate").assert_awaited_once_with(57600)
    finally:
        state.cleanup()
