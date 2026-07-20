"""Comprehensive unit tests for mcubridge_client (cli, env, spi, definitions, and __init__). [SIL-2]"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcubridge_client import (
    Bridge,
    SpiBitOrder,
    SpiDevice,
    SpiMode,
    build_bridge_args,
    dump_client_env,
)
from mcubridge_client.cli import bridge_session, configure_logging
from mcubridge_client.env import _is_openwrt, read_uci_general

# ==============================================================================
# cli.py & env.py tests
# ==============================================================================


def test_cli_configure_logging() -> None:
    """configure_logging sets up basic logging without raising exceptions."""
    configure_logging()
    logging.getLogger("test").info("logging configured")


@pytest.mark.asyncio
async def test_cli_bridge_session() -> None:
    """bridge_session context manager connects and disconnects Bridge."""
    mock_connect = AsyncMock()
    mock_disconnect = AsyncMock()

    with patch.object(Bridge, "connect", mock_connect):
        with patch.object(Bridge, "disconnect", mock_disconnect):
            async with bridge_session("/tmp/test.sock", "br") as b:
                assert isinstance(b, Bridge)
                assert b.socket_path == "/tmp/test.sock"
    mock_connect.assert_awaited_once()
    mock_disconnect.assert_awaited_once()


def test_env_is_openwrt() -> None:
    """_is_openwrt checks environment variable and file presence."""
    with patch.dict("os.environ", {"MCUBRIDGE_FORCE_UCI": "1"}):
        assert _is_openwrt() is True

    with patch.dict("os.environ", {}, clear=True):
        with patch("pathlib.Path.exists", return_value=True):
            assert _is_openwrt() is True


def test_env_read_uci_general() -> None:
    """read_uci_general returns UCI config dict or empty dict."""
    with patch("mcubridge_client.env._is_openwrt", return_value=False):
        assert read_uci_general() == {}

    with patch("mcubridge_client.env._is_openwrt", return_value=True):
        with patch("importlib.util.find_spec", return_value=MagicMock()):
            with patch("importlib.import_module") as mock_imp:
                mock_mod = MagicMock()
                mock_mod.get_uci_config = MagicMock(return_value={"socket_path": "/var/run/test.sock", "_private": "x"})
                mock_imp.return_value = mock_mod
                res = read_uci_general()
                assert res == {"socket_path": "/var/run/test.sock"}

                # Exception path in get_uci_config
                mock_mod.get_uci_config.side_effect = RuntimeError("UCI error")
                assert read_uci_general() == {}


def test_env_dump_client_env(capsys: pytest.CaptureFixture[str]) -> None:
    """dump_client_env outputs snapshot to logger or stdout."""
    # 1. Custom logger
    mock_log = MagicMock()
    dump_client_env(mock_log)
    assert mock_log.info.call_count >= 2

    # 2. Stdout fallback
    dump_client_env(None)
    captured = capsys.readouterr()
    assert "socket_path=" in captured.out


# ==============================================================================
# spi.py & definitions.py tests
# ==============================================================================


def test_definitions_build_bridge_args() -> None:
    """build_bridge_args builds dictionary from parameters."""
    assert build_bridge_args() == {"topic_prefix": "br"}
    assert build_bridge_args("/tmp/sock", "test_prefix") == {
        "socket_path": "/tmp/sock",
        "topic_prefix": "test_prefix",
    }


@pytest.mark.asyncio
async def test_spi_device_lifecycle_and_transfer() -> None:
    """SpiDevice context manager, properties, begin/end, and transfer."""
    mock_bridge = MagicMock(spec=Bridge)
    mock_bridge.spi_begin = AsyncMock()
    mock_bridge.spi_config = AsyncMock()
    mock_bridge.spi_end = AsyncMock()
    mock_bridge.spi_transfer = AsyncMock(return_value=b"spi_response")

    dev = SpiDevice(mock_bridge, frequency=2000000, bit_order=SpiBitOrder.LSBFIRST, mode=SpiMode.MODE1)

    assert dev.frequency == 2000000
    assert dev.bit_order == SpiBitOrder.LSBFIRST
    assert dev.mode == SpiMode.MODE1

    async with dev as active_dev:
        assert active_dev is dev
        mock_bridge.spi_begin.assert_awaited_once()
        mock_bridge.spi_config.assert_awaited_once_with(frequency=2000000, bit_order=0, data_mode=1)

        # Idempotent begin
        await dev.begin()
        assert mock_bridge.spi_begin.call_count == 1

        # Transfer with bytes and Sequence[int]
        res1 = await dev.transfer(b"\x01\x02")
        assert res1 == b"spi_response"

        res2 = await dev.transfer([1, 2, 3])
        assert res2 == b"spi_response"

    mock_bridge.spi_end.assert_awaited_once()

    # Idempotent end
    await dev.end()
    assert mock_bridge.spi_end.call_count == 1


# ==============================================================================
# __init__.py (Bridge client methods) tests
# ==============================================================================


@pytest.mark.asyncio
async def test_bridge_client_methods() -> None:
    """Bridge client declarative API method coverage."""
    b = Bridge(topic_prefix="br", socket_path="/var/run/test.sock")
    assert b.socket_path == "/var/run/test.sock"

    # Mock stub and channel
    mock_stub = MagicMock()
    mock_stub.Publish = AsyncMock()
    mock_stub.SubscribeConsole = MagicMock()

    b.stub = mock_stub
    b.channel = MagicMock()

    # 1. Console Write and Read
    await b.console_write("hello console")
    await b.console_write(b"raw bytes")
    b._console_queue.put_nowait(b"async input")
    msg = await b.console_read_async()
    assert msg == "async input"

    # Console read decode error fallback & timeout
    b._console_queue.put_nowait(b"\xff\xfe")
    assert "<hex:fffe>" in (await b.console_read_async() or "")
    assert await b.console_read_async() is None

    # 2. Digital & Analog Writes & Modes
    await b.digital_write(13, 1)
    await b.analog_write(2, 255)
    await b.set_digital_mode(13, 1)

    # 3. Mailbox Write & Read
    await b.mailbox_write("msg string")
    await b.mailbox_write(b"msg bytes")

    # 4. System & SPI control
    await b.enter_bootloader()
    await b.spi_begin()
    await b.spi_end()
    await b.spi_config(4000000, 1, 0)
    dev = b.spi(8000000, 1, 0)
    assert isinstance(dev, SpiDevice)

    # 5. File remove
    await b.file_remove("/sd/test.txt")


@pytest.mark.asyncio
async def test_bridge_publish_and_wait_responses() -> None:
    """Bridge _publish_and_wait mock responses for high-level operations."""
    b = Bridge()
    mock_stub = MagicMock()
    b.stub = mock_stub
    b.channel = MagicMock()

    # Mock Publish returning bytes
    from mcubridge_client import mcubridge_pb2 as client_pb

    # Digital & Analog Read
    mock_stub.Publish = AsyncMock(return_value=client_pb.CloudQueuedPublish(payload=b"1"))
    val_d = await b.digital_read(13)
    assert val_d == 1

    mock_stub.Publish = AsyncMock(return_value=client_pb.CloudQueuedPublish(payload=b"512"))
    val_a = await b.analog_read(1)
    assert val_a == 512

    # Datastore get/put
    mock_stub.Publish = AsyncMock(return_value=client_pb.CloudQueuedPublish(payload=b"val"))
    await b.put("key1", "val1")
    gval = await b.get("key1")
    assert gval == "val"

    # Shell run_async & poll
    proc_pb = client_pb.ProcessRunAsyncResponse(pid=42).SerializeToString()
    mock_stub.Publish = AsyncMock(return_value=client_pb.CloudQueuedPublish(payload=proc_pb))
    pid = await b.run_shell_command_async(["ls", "-la"])
    assert pid == 42

    poll_pb = client_pb.ProcessPollResponse(
        status=0, exit_code=0, stdout_data=b"out", stderr_data=b"", finished=True
    ).SerializeToString()
    mock_stub.Publish = AsyncMock(return_value=client_pb.CloudQueuedPublish(payload=poll_pb))
    poll_res = await b.poll_shell_process(42)
    assert poll_res["finished"] is True
    assert poll_res["stdout_chunk"] == b"out"

    # File write & read
    mock_stub.Publish = AsyncMock(return_value=client_pb.CloudQueuedPublish(payload=b"file_data"))
    await b.file_write("test.txt", "content")
    fdata = await b.file_read("test.txt")
    assert fdata == b"file_data"

    # Mailbox read
    mock_stub.Publish = AsyncMock(return_value=client_pb.CloudQueuedPublish(payload=b"mb_msg"))
    mb_msg = await b.mailbox_read()
    assert mb_msg == b"mb_msg"

    # Mailbox read timeout error -> returns None
    mock_stub.Publish = AsyncMock(side_effect=TimeoutError)
    mb_timeout = await b.mailbox_read(0.01)
    assert mb_timeout is None

    # Free memory
    mock_stub.Publish = AsyncMock(return_value=client_pb.CloudQueuedPublish(payload=b"4096"))
    mem = await b.get_free_memory()
    assert mem == 4096

    # SPI transfer
    mock_stub.Publish = AsyncMock(return_value=client_pb.CloudQueuedPublish(payload=b"\xaa\xbb"))
    spi_resp = await b.spi_transfer(b"\x11\x22")
    assert spi_resp == b"\xaa\xbb"


@pytest.mark.asyncio
async def test_bridge_connection_not_connected_and_context_manager() -> None:
    """Bridge raises ConnectionError when not connected and handles context manager."""
    b = Bridge()
    with pytest.raises(ConnectionError):
        await b.digital_write(13, 1)

    with pytest.raises(ConnectionError):
        await b.digital_read(13)

    # Context manager connect/disconnect
    with patch.object(b, "connect", new=AsyncMock()) as mock_c:
        with patch.object(b, "disconnect", new=AsyncMock()) as mock_d:
            async with b as session:
                assert session is b
            mock_c.assert_awaited_once()
            mock_d.assert_awaited_once()
