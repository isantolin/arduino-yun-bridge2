import contextlib
import io
import pytest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import msgspec
from mcubridge.daemon import app
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.services.spi import SpiComponent
from mcubridge.services.file import FileComponent
from mcubridge.protocol.protocol import Command
from mcubridge.protocol.topics import parse_topic


from typing import Any, Callable


class _CliRunner:
    def invoke(
        self, func: Callable[[list[str]], Any], args: list[str]
    ) -> SimpleNamespace:
        buf = io.StringIO()
        exit_code = 0
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                func(args)
        except SystemExit as e:
            exit_code = int(e.code) if isinstance(e.code, int) else 1
        except Exception:
            exit_code = 1
        return SimpleNamespace(exit_code=exit_code, output=buf.getvalue())


def test_daemon_uvloop_missing():
    runner = _CliRunner()
    with patch("mcubridge.daemon.uvloop", None):
        result = runner.invoke(app, ["--serial-port", "/dev/ttyUSB0"])
    assert result.exit_code == 1


def test_daemon_crypto_failure():
    runner = _CliRunner()
    with patch("mcubridge.daemon.verify_crypto_integrity", return_value=False):
        result = runner.invoke(app, ["--serial-port", "/dev/ttyUSB0"])
    assert result.exit_code == 1


@pytest.mark.asyncio
async def test_mailbox_handle_processed_malformed():
    mqtt_mock = AsyncMock()
    mailbox = MailboxComponent(MagicMock(), MagicMock(), AsyncMock(), mqtt_mock)
    # Malformed msgpack (too short for packet type)
    await mailbox.handle_processed(1, b"\x90")
    assert mqtt_mock.enqueue_mqtt.called


@pytest.mark.asyncio
async def test_mailbox_handle_push_malformed():
    mailbox = MailboxComponent(MagicMock(), MagicMock(), AsyncMock(), AsyncMock())
    res = await mailbox.handle_push(1, b"\xff")
    assert res is False


@pytest.mark.asyncio
async def test_mailbox_handle_push_overflow():
    state = MagicMock()
    state.mailbox_incoming_queue = deque([b"old"])
    state.mailbox_queue_limit = 1
    state.mqtt_topic_prefix = "br"
    mailbox = MailboxComponent(MagicMock(), state, AsyncMock(), AsyncMock())

    from mcubridge.protocol.structures import MailboxPushPacket

    payload = msgspec.msgpack.encode(MailboxPushPacket(data=b"new"))
    await mailbox.handle_push(1, payload)
    assert len(state.mailbox_incoming_queue) == 1
    assert state.mailbox_incoming_queue[0] == b"new"


@pytest.mark.asyncio
async def test_mailbox_handle_available_malformed():
    mailbox = MailboxComponent(MagicMock(), MagicMock(), AsyncMock(), AsyncMock())
    res = await mailbox.handle_available(1, b"\x01")
    assert res is False


@pytest.mark.asyncio
async def test_mailbox_handle_read_empty():
    state = MagicMock()
    state.mailbox_queue = []
    serial_mock = AsyncMock()
    mailbox = MailboxComponent(MagicMock(), state, serial_mock, AsyncMock())
    await mailbox.handle_read(1, b"")
    # Verify it sends empty response
    args = serial_mock.send.call_args
    assert args[0][0] == Command.CMD_MAILBOX_READ_RESP.value


@pytest.mark.asyncio
async def test_spi_handle_transfer_resp_malformed():
    spi = SpiComponent(MagicMock(), MagicMock(), AsyncMock(), AsyncMock())
    res = await spi.handle_transfer_resp(1, b"\xff")
    assert res is False


@pytest.mark.asyncio
async def test_spi_handle_mqtt_malformed_config():
    spi = SpiComponent(MagicMock(), MagicMock(), AsyncMock(), AsyncMock())
    route = parse_topic("br", "br/spi/config")
    assert route is not None
    msg = MagicMock()
    msg.payload = b"not json"
    res = await spi.handle_mqtt(route, msg)
    assert res is False


@pytest.mark.asyncio
async def test_file_handle_read_not_found():
    serial_mock = AsyncMock()
    file_svc = FileComponent(MagicMock(), MagicMock(), serial_mock, AsyncMock())
    with patch.object(file_svc, "_get_safe_path", return_value=None):
        from mcubridge.protocol.structures import FileReadPacket

        await file_svc.handle_read(
            1, msgspec.msgpack.encode(FileReadPacket(path="foo"))
        )
        assert serial_mock.send.called


@pytest.mark.asyncio
async def test_file_handle_remove_fail():
    file_svc = FileComponent(MagicMock(), MagicMock(), AsyncMock(), AsyncMock())
    with patch.object(file_svc, "_get_safe_path", return_value=None):
        from mcubridge.protocol.structures import FileRemovePacket

        res = await file_svc.handle_remove(
            1, msgspec.msgpack.encode(FileRemovePacket(path="foo"))
        )
        assert res is False
