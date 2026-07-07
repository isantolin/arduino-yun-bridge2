import asyncio
from unittest.mock import MagicMock, patch
from pathlib import Path
from typing import Any

import pytest


from mcubridge.protocol import protocol

from mcubridge.state.status import status_writer
import mcubridge.state.status as status_mod
from mcubridge.state.context import create_runtime_state
from mcubridge.config.logging import configure_logging, hexdump_processor
from mcubridge.config.settings import RuntimeConfig
from mcubridge.security.security import (
    secure_zero,
    generate_nonce_with_counter,
    extract_nonce_counter,
    validate_nonce_counter,
)


@pytest.mark.asyncio
async def test_status_writer_coverage() -> None:
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/ttytest",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=9600,
        cloud_host="localhost",
        cloud_port=1883,
    )
    state = create_runtime_state(config)
    try:
        task = asyncio.create_task(status_writer(state, 1))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        state.cleanup()


def test_write_status_file_errors() -> None:
    with patch("mcubridge.state.status.STATUS_FILE") as mock_file:
        mock_file.parent = MagicMock()
        mock_file.parent.mkdir.side_effect = OSError("Perm denied")
        with patch("mcubridge.state.status.logger") as mock_logger:
            getattr(status_mod, "_write_status_file")({})
            mock_logger.error.assert_called()


def test_logging_coverage() -> None:
    config = RuntimeConfig(
        topic_prefix="br",
        serial_port="/dev/ttytest",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=9600,
        cloud_host="localhost",
        cloud_port=1883,
        debug=True,
    )
    configure_logging(config)
    event: dict[str, Any] = {"data": b"\x01\x02", "empty": b""}
    processed = hexdump_processor(None, "info", event)
    assert processed["data"] == "[01 02]"
    assert processed["empty"] == "[]"


def test_security_primitives_coverage() -> None:
    ba = bytearray(b"sensitive")
    secure_zero(ba)
    assert ba == bytearray(len(b"sensitive"))
    mv = memoryview(bytearray(b"sensitive"))
    secure_zero(mv)
    assert mv == bytearray(len(b"sensitive"))
    nonce, next_c = generate_nonce_with_counter(10)
    assert next_c == 11
    assert extract_nonce_counter(nonce) == 11
    ok, cur = validate_nonce_counter(nonce, 10)
    assert ok
    assert cur == 11
    ok, cur = validate_nonce_counter(nonce, 11)
    assert not ok
    with pytest.raises(ValueError, match="Nonce counter overflow"):
        generate_nonce_with_counter(protocol.NONCE_COUNTER_MASK)
    with pytest.raises(ValueError, match="Nonce counter overflow"):
        generate_nonce_with_counter(-1)


def test_dec_hook_extra_paths() -> None:
    import mcubridge.config.settings as settings_mod

    _coerce_value = getattr(settings_mod, "_coerce_value")
    from google.protobuf.descriptor import FieldDescriptor

    with patch("pathlib.Path.expanduser", return_value=Path("/home/user/test")):
        res = _coerce_value("~/test", FieldDescriptor.TYPE_STRING, "some_dir")
        assert res == "/home/user/test"
    res = _coerce_value("/tmp/test", FieldDescriptor.TYPE_STRING)
    assert res == "/tmp/test"
