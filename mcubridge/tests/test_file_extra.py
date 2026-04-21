"""Extra edge-case tests for FileComponent (SIL-2)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.services.file import FileComponent
from mcubridge.state.context import create_runtime_state


@pytest.mark.asyncio
async def test_file_do_write_large_warning() -> None:
    from mcubridge.services.file import _do_write_file  # type: ignore[reportPrivateUsage]

    import tempfile

    # We use a real temp dir to avoid quota issues with large files
    with tempfile.TemporaryDirectory(prefix="mcubridge-test-large-") as tmpdir:
        path = Path(tmpdir) / "large.bin"
        # 1MB + 1 byte
        data = b"\x00" * (1024 * 1024 + 1)

        # Should log a warning, but we just verify it doesn't crash
        _do_write_file(path, data)
        assert path.stat().st_size > 1024 * 1024


@pytest.mark.asyncio
async def test_file_refresh_storage_usage_handles_oserror() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        serial_flow = MagicMock()
        serial_flow.send = AsyncMock(return_value=True)
        mqtt_flow = MagicMock()
        mqtt_flow.publish = AsyncMock()

        with patch("mcubridge.transport.mqtt.MqttTransport.publish", new_callable=AsyncMock):  # type: ignore[reportUnusedVariable]
            comp = FileComponent(config, state, serial_flow, mqtt_flow)

            def boom(*_args: Any, **_kwargs: Any) -> Any:
                raise OSError("Permission denied")

            with patch("pathlib.Path.rglob", side_effect=boom):
                await comp._refresh_storage_usage()  # type: ignore[reportPrivateUsage]
                assert state.file_storage_bytes_used == 0
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_file_remove_with_tracking_not_a_file(tmp_path: Path) -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=str(tmp_path),
    )
    state = create_runtime_state(config)
    try:
        serial_flow = MagicMock()
        mqtt_flow = MagicMock()

        comp = FileComponent(config, state, serial_flow, mqtt_flow)

        # Test with directory
        d = tmp_path / "dir"
        d.mkdir()
        result = await comp._remove_with_tracking(d)  # type: ignore[reportPrivateUsage]
        assert result is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_file_handle_read_response_no_pending() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        serial_flow = MagicMock()
        mqtt_flow = MagicMock()

        comp = FileComponent(config, state, serial_flow, mqtt_flow)

        # No pending request set
        result = await comp.handle_read_response(0, b"\x00")
        assert result is False
    finally:
        state.cleanup()


@pytest.mark.asyncio
async def test_file_handle_read_response_malformed() -> None:
    config = RuntimeConfig(
        serial_shared_secret=b"secret_1234",
        file_system_root=f"/tmp/mcubridge-test-{os.getpid()}-{time.time_ns()}",
    )
    state = create_runtime_state(config)
    try:
        serial_flow = MagicMock()
        mqtt_flow = MagicMock()

        comp = FileComponent(config, state, serial_flow, mqtt_flow)

        import asyncio
        from mcubridge.services.file import _PendingMcuRead  # type: ignore[reportPrivateUsage]

        pending = _PendingMcuRead(identifier="test", future=asyncio.get_running_loop().create_future())
        comp._pending_mcu_read = pending  # type: ignore[reportPrivateUsage]

        result = await comp.handle_read_response(0, b"\xff\xff")
        assert result is False
        assert pending.future.done()
        assert isinstance(pending.future.exception(), ValueError)
    finally:
        state.cleanup()
