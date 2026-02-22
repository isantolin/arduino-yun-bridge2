"""Extra coverage for mcubridge.services.file."""

from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol.protocol import Status
from mcubridge.services.file import FileComponent, _do_write_file
from mcubridge.state.context import create_runtime_state


def test_file_do_write_large_warning(tmp_path: Path) -> None:
    test_file = tmp_path / "large.bin"
    # FILE_LARGE_WARNING_BYTES is 1MB
    data = b"A" * (1024 * 1024 + 1)
    with patch("mcubridge.services.file.logger.warning") as mock_warn:
        _do_write_file(test_file, data)
        mock_warn.assert_called()


@pytest.mark.asyncio
async def test_file_handle_write_malformed() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    fc = FileComponent(config, state, AsyncMock())
    assert await fc.handle_write(b"") is False


@pytest.mark.asyncio
async def test_file_handle_write_traversal() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = AsyncMock()
    fc = FileComponent(config, state, ctx)

    from mcubridge.protocol.structures import FileWritePacket
    # Path traversal
    payload = FileWritePacket(path="../etc/passwd", data=b"data").encode()
    assert await fc.handle_write(payload) is False
    ctx.send_frame.assert_called_with(Status.ERROR.value, ANY) # INVALID_PATH


@pytest.mark.asyncio
async def test_file_handle_write_absolute() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    ctx = AsyncMock()
    fc = FileComponent(config, state, ctx)

    from mcubridge.protocol.structures import FileWritePacket
    payload = FileWritePacket(path="/tmp/foo", data=b"data").encode()
    assert await fc.handle_write(payload) is False


@pytest.mark.asyncio
async def test_file_handle_read_malformed() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    fc = FileComponent(config, state, AsyncMock())
    await fc.handle_read(b"")


@pytest.mark.asyncio
async def test_file_handle_remove_malformed() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    fc = FileComponent(config, state, AsyncMock())
    assert await fc.handle_remove(b"") is False


@pytest.mark.asyncio
async def test_file_handle_mqtt_unknown() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    fc = FileComponent(config, state, MagicMock())
    await fc.handle_mqtt("unknown", ["path"], b"")


@pytest.mark.asyncio
async def test_file_perform_operation_unknown() -> None:
    config = RuntimeConfig(serial_shared_secret=b"secret_1234")
    state = create_runtime_state(config)
    fc = FileComponent(config, state, MagicMock())
    # bypass safe path check by mocking it to return something
    with patch.object(fc, "_get_safe_path", return_value=Path("/tmp/foo")):
        success, content, reason = await fc._perform_file_operation("unknown", "foo")
        assert success is False
        assert reason == "unknown_operation"
