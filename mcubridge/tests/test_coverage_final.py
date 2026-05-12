import asyncio
import time
from unittest.mock import MagicMock, patch
from pathlib import Path
from typing import Any

import pytest

from mcubridge.protocol.spec_model import ProtocolSpec
from mcubridge.state.status import status_writer, _write_status_file
from mcubridge.state.context import create_runtime_state
from mcubridge.config.logging import configure_logging, hexdump_processor
from mcubridge.config.settings import RuntimeConfig
from mcubridge.policy import tokenize_shell_command, CommandValidationError
from mcubridge.security.security import (
    secure_zero,
    secure_zero_bytes_copy,
    generate_nonce_with_counter,
    extract_nonce_counter,
    validate_nonce_counter,
    aead_encrypt,
    aead_decrypt,
    verify_crypto_integrity,
)


def test_protocol_spec_load(tmp_path: Path) -> None:
    spec_file = tmp_path / "spec.toml"
    content = """
constants = { VERSION = 1 }
hardware = { TYPE = "arduino" }
commands = []
statuses = []
handshake = {}
mqtt_subscriptions = []
actions = []
topics = []
capabilities = {}
architectures = {}
compression = {}
data_formats = {}
mqtt_suffixes = {}
mqtt_defaults = {}
status_reasons = {}
"""
    spec_file.write_text(content)
    spec = ProtocolSpec.load(spec_file)
    assert spec.constants["VERSION"] == 1


@pytest.mark.asyncio
async def test_status_writer_coverage() -> None:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=115200,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
    )
    state = create_runtime_state(config)

    mock_child = MagicMock()
    mock_child.pid = 1234
    mock_child.name.return_value = "child"
    mock_child.cpu_percent.return_value = 0.1
    mock_child.memory_info.return_value.rss = 1000

    with patch("psutil.Process") as mock_proc:
        mock_proc.return_value.children.return_value = [mock_child]

        task = asyncio.create_task(status_writer(state, 1))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_write_status_file_errors() -> None:
    with patch("mcubridge.state.status.STATUS_FILE") as mock_file:
        # Use a real path for mkdir mock to avoid confusion
        mock_file.parent = MagicMock()
        mock_file.parent.mkdir.side_effect = OSError("Perm denied")
        with patch("mcubridge.state.status.logger") as mock_logger:
            _write_status_file({})
            mock_logger.error.assert_called()


def test_logging_coverage() -> None:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=115200,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
        debug=True,
    )
    configure_logging(config)

    # Test hexdump processor
    # Use real bytes
    event: dict[str, Any] = {"data": b"\x01\x02", "empty": b""}
    processed = hexdump_processor(None, "info", event)
    assert processed["data"] == "[01 02]"
    assert processed["empty"] == "[]"


def test_policy_coverage() -> None:
    assert tokenize_shell_command("ls -la") == ("ls", "-la")
    with pytest.raises(CommandValidationError):
        tokenize_shell_command("")
    with pytest.raises(CommandValidationError):
        tokenize_shell_command("  ")
    with pytest.raises(CommandValidationError):
        tokenize_shell_command("'unclosed quote")


def test_security_primitives_coverage() -> None:
    # secure_zero
    ba = bytearray(b"sensitive")
    secure_zero(ba)
    assert ba == bytearray(len(b"sensitive"))

    mv = memoryview(bytearray(b"sensitive"))
    secure_zero(mv)
    assert mv == bytearray(len(b"sensitive"))

    # secure_zero_bytes_copy
    assert secure_zero_bytes_copy(b"abc") == b"\x00\x00\x00"

    # Nonce functions
    nonce, next_c = generate_nonce_with_counter(10)
    assert next_c == 11
    assert extract_nonce_counter(nonce) == 11

    ok, cur = validate_nonce_counter(nonce, 10)
    assert ok is True
    assert cur == 11

    ok, cur = validate_nonce_counter(nonce, 11)
    assert ok is False

    # AEAD
    key = b"A" * 32
    data = b"hello"
    ad = b"header"
    ct = aead_encrypt(key, nonce, data, ad)
    pt = aead_decrypt(key, nonce, ct, ad)
    assert pt == data

    # verify_crypto_integrity
    assert verify_crypto_integrity() is True


def test_state_context_extra_coverage() -> None:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=115200,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
    )
    state = create_runtime_state(config)
    # Correct method name
    state.mark_supervisor_healthy("test")
    state.apply_handshake_stats(
        {"attempts": 1, "successes": 1, "last_unix": time.time()}
    )
    # Use public API if available or suppress if necessary
    # For coverage tests, private access is sometimes tolerated but we can cast to Any
    # to satisfy the type checker for now while maintaining the test's intent.
    state_any = state  # type: Any
    state_any._apply_spool_observation({"corrupt_dropped": 1, "dropped_due_to_limit": 1})
    assert state.handshake_duration_since_start() >= 0
