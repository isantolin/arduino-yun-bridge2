"""Final coverage tests to reach 100%."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
import msgspec

from mcubridge.config import logging as logging_config
from mcubridge.config import settings
from mcubridge.protocol import rle
from mcubridge.services.console import ConsoleComponent
from mcubridge.services.datastore import DatastoreComponent, DatastoreAction
from mcubridge.services.file import FileComponent, FileAction
from mcubridge.services.mailbox import MailboxComponent
from mcubridge.services.pin import PinComponent
from mcubridge.protocol.protocol import Command
from mcubridge.state.context import RuntimeState
from mcubridge.protocol.topics import Topic
from mcubridge.util import chunk_bytes


@pytest.fixture
def real_config():
    raw = settings.get_default_config()
    raw["serial_shared_secret"] = b"abcd1234"
    raw["serial_retry_timeout"] = 1.0
    raw["serial_response_timeout"] = 2.0
    raw["serial_handshake_fatal_failures"] = 15
    raw["process_max_concurrent"] = 4
    config = msgspec.convert(raw, settings.RuntimeConfig, strict=False)
    return config


def test_logging_config_candidates_branch():
    with patch("mcubridge.config.logging.SYSLOG_SOCKET", Path("/not/dev/log")):
        handler = logging_config._build_handler()
        assert isinstance(handler, (logging.Handler))


def test_settings_validation_errors_coverage():
    raw = settings.get_default_config()
    raw["serial_shared_secret"] = b"aaaaaaaa"
    with pytest.raises(msgspec.ValidationError, match="four distinct bytes"):
        msgspec.convert(raw, settings.RuntimeConfig, strict=False)

    raw = settings.get_default_config()
    raw["serial_shared_secret"] = b"abcd1234"
    raw["mailbox_queue_limit"] = 100
    raw["mailbox_queue_bytes_limit"] = 50
    with pytest.raises(msgspec.ValidationError, match="mailbox_queue_bytes_limit must be greater"):
        msgspec.convert(raw, settings.RuntimeConfig, strict=False)


def test_rle_decode_long_run_branch():
    data = b"AAAAA"
    encoded = rle.encode(data)
    decoded = rle.decode(encoded)
    assert decoded == data


@pytest.mark.asyncio
async def test_console_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    comp = ConsoleComponent(real_config, runtime_state, ctx)
    with patch("mcubridge.services.console.chunk_bytes", return_value=[b"a", b""]):
        runtime_state.mcu_is_paused = True
        await comp.handle_mqtt_input(b"payload")
        assert any(b"a" == c for c in runtime_state.console_to_mcu_queue)
    runtime_state.mcu_is_paused = False
    with patch("mcubridge.services.console.chunk_bytes", return_value=[b""]):
        await comp.handle_mqtt_input(b"payload")


@pytest.mark.asyncio
async def test_console_component_async_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=False)
    comp = ConsoleComponent(real_config, runtime_state, ctx)
    runtime_state.mcu_is_paused = False
    await comp.handle_mqtt_input(b"payload")
    assert runtime_state.console_to_mcu_queue
    runtime_state.console_to_mcu_queue.clear()
    runtime_state.enqueue_console_chunk(b"abc", logging.getLogger())
    with patch("mcubridge.services.console.chunk_bytes", return_value=[b""]):
        await comp.flush_queue()
    runtime_state.console_to_mcu_queue.clear()
    runtime_state.enqueue_console_chunk(b"abc", logging.getLogger())
    ctx.send_frame = AsyncMock(return_value=False)
    with patch("mcubridge.services.console.chunk_bytes", return_value=[b"a"]):
        await comp.flush_queue()
    assert chunk_bytes(b"", 1) == []


@pytest.mark.asyncio
async def test_datastore_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.publish = AsyncMock(return_value=True)
    ctx.enqueue_mqtt = AsyncMock(return_value=True)
    comp = DatastoreComponent(real_config, runtime_state, ctx)

    # Truly invalid payload for Put
    payload = b"\x01"
    await comp.handle_put(payload) # Validate call

    await comp.handle_get_request(b"")
    await comp.handle_get_request(b"\x05k")

    runtime_state.datastore["big"] = "a" * 300
    await comp.handle_get_request(bytes([3]) + b"big")
    await comp.handle_mqtt(DatastoreAction.GET, [], b"", "")
    await comp.handle_mqtt("UNKNOWN", ["key"], b"", "")
    await comp._handle_mqtt_put("k" * 300, "v", None)
    await comp._handle_mqtt_put("k", "v" * 300, None)


@pytest.mark.asyncio
async def test_file_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.publish = AsyncMock(return_value=True)
    ctx.enqueue_mqtt = AsyncMock(return_value=True)
    comp = FileComponent(real_config, runtime_state, ctx)

    await comp.handle_remove(b"")
    await comp.handle_remove(b"\x05f")

    with patch.object(comp, "_perform_file_operation", return_value=(False, None, "error")):
        await comp.handle_mqtt(FileAction.READ, ["file.txt"], b"")

    assert comp._normalise_filename("..") is None
    with patch("pathlib.Path.mkdir", side_effect=OSError("perm")):
        assert comp._get_base_dir() is None

    runtime_state.file_write_max_bytes = 10
    res = await comp._write_with_quota(Path("/tmp/f"), b"a" * 20)
    assert res[0] is False


@pytest.mark.asyncio
async def test_mailbox_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.publish = AsyncMock(return_value=True)
    ctx.enqueue_mqtt = AsyncMock(return_value=True)

    comp = MailboxComponent(real_config, runtime_state, ctx)
    await comp.handle_processed(b"a")
    assert await comp.handle_push(b"a") is False


@pytest.mark.asyncio
async def test_pin_component_gaps(runtime_state: RuntimeState, real_config):
    ctx = MagicMock()
    ctx.send_frame = AsyncMock(return_value=True)
    ctx.publish = AsyncMock(return_value=True)
    ctx.enqueue_mqtt = AsyncMock(return_value=True)
    comp = PinComponent(real_config, runtime_state, ctx)

    await comp.handle_unexpected_mcu_request(Command.CMD_ANALOG_READ, b"")
    await comp.handle_analog_read_resp(b"a")
    await comp.handle_analog_read_resp(b"ab")

    await comp.handle_mqtt("d", ["br", "d"], "1")
    ctx.send_frame = AsyncMock(return_value=False)
    await comp._handle_read_command(Topic.DIGITAL, 3)

    assert comp._parse_pin_identifier("invalid") == -1
    assert comp._parse_pin_value(Topic.DIGITAL, "abc") is None
