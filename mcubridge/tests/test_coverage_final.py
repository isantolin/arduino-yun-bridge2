import asyncio
from unittest.mock import MagicMock, patch
from pathlib import Path
from typing import Any, Union

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
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
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
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
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
    assert ok is True
    assert cur == 11
    ok, cur = validate_nonce_counter(nonce, 11)
    assert ok is False
    with pytest.raises(ValueError, match="Nonce counter overflow"):
        generate_nonce_with_counter(protocol.NONCE_COUNTER_MASK)
    with pytest.raises(ValueError, match="Nonce counter overflow"):
        generate_nonce_with_counter(-1)


def test_dec_hook_extra_paths() -> None:
    import mcubridge.config.settings as settings_mod

    _dec_hook = getattr(settings_mod, "_dec_hook")

    with patch("pathlib.Path.expanduser", return_value=Path("/home/user/test")):
        res = _dec_hook(str, "~/test")
        assert res == "/home/user/test"
    res = _dec_hook(str, "/tmp/test")
    assert res == "/tmp/test"
    with pytest.raises(TypeError, match="Cannot coerce"):
        _dec_hook(int, "not_an_int")
    res = _dec_hook(Union[bytes, str], " secret ")
    assert res == b"secret"
    res = _dec_hook(tuple, "a b c")
    assert res == ("a", "b", "c")


@pytest.mark.asyncio
async def test_status_writer_error_handling() -> None:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/test",
        serial_shared_secret="secret1234",
    )
    state = create_runtime_state(config)
    with patch("mcubridge.state.status._write_status_file", side_effect=RuntimeError("write failed")):
        from mcubridge.state.status import status_writer

        task = asyncio.create_task(status_writer(state, 1))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    with patch("mcubridge.state.status.MessageToJson", side_effect=ValueError("encode error")):
        _write_status_file = getattr(status_mod, "_write_status_file")

        _write_status_file({})


def test_daemon_metrics_initialization() -> None:
    from mcubridge.state.metrics import DaemonMetrics
    from prometheus_client import CollectorRegistry

    reg = CollectorRegistry()
    metrics = DaemonMetrics(reg)
    assert metrics.registry == reg
    metrics.mqtt_messages_published.inc()
