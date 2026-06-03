import asyncio
import time
from unittest.mock import MagicMock, patch
from pathlib import Path
from typing import Any

import pytest

from mcubridge.protocol import protocol
from mcubridge.protocol.spec_model import ProtocolSpec

from mcubridge.state.status import status_writer
import mcubridge.state.status as status_mod
from mcubridge.state.context import create_runtime_state
from mcubridge.config.logging import configure_logging, hexdump_processor
from mcubridge.config.settings import RuntimeConfig
from mcubridge.policy import tokenize_shell_command, CommandValidationError
from mcubridge.security.security import (
    secure_zero,
    generate_nonce_with_counter,
    extract_nonce_counter,
    validate_nonce_counter,
    verify_crypto_integrity,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


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
        serial_baud=protocol.DEFAULT_BAUDRATE,
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

    # Nonce functions
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

    from mcubridge.security import security
    from mcubridge.security.security import NONCE_RANDOM_BYTES
    import secrets

    nonce_struct = getattr(security, "_FULL_NONCE_STRUCT")
    nonce_zero = nonce_struct.pack(secrets.token_bytes(NONCE_RANDOM_BYTES), 0)

    ok, cur = validate_nonce_counter(nonce_zero, protocol.NONCE_COUNTER_MASK)
    assert ok is False

    # AEAD
    key = b"A" * 32
    data = b"hello"
    ad = b"header"
    cipher = ChaCha20Poly1305(key)
    ct = cipher.encrypt(nonce, data, ad)
    pt = cipher.decrypt(nonce, ct, ad)
    assert pt == data

    # verify_crypto_integrity
    assert verify_crypto_integrity() is True


def test_state_context_extra_coverage() -> None:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
    )
    state = create_runtime_state(config)
    # Correct method name
    state.mark_supervisor_healthy("test")
    state.apply_handshake_stats({"attempts": 1, "successes": 1, "last_unix": time.time()})
    # Use public API if available or suppress if necessary
    # For coverage tests, private access is sometimes tolerated but we can cast to Any
    # to satisfy the type checker for now while maintaining the test's intent.
    getattr(state, "_apply_spool_observation")({"corrupt_dropped": 1, "dropped_due_to_limit": 1})
    assert state.handshake_duration_since_start() >= 0


def test_mark_transport_disconnected_clears_sync_event() -> None:
    config = RuntimeConfig(
        mqtt_topic="br",
        serial_port="/dev/ttytest",
        serial_baud=protocol.DEFAULT_BAUDRATE,
        serial_safe_baud=9600,
        mqtt_host="localhost",
        mqtt_port=1883,
    )
    state = create_runtime_state(config)
    state.mark_synchronized()
    assert state.link_sync_event.is_set()

    state.mark_transport_disconnected()
    assert state.state == "disconnected"
    assert not state.link_sync_event.is_set()


def test_security_aead_encrypt_decrypt() -> None:
    from mcubridge.security.security import aead_encrypt, aead_decrypt

    key = b"B" * 32
    nonce = b"\x02" * 12
    payload = b"secret_data"
    ad = b"additional_data"

    ciphertext, tag = aead_encrypt(payload, key, nonce, ad)
    assert len(tag) == 16
    assert ciphertext != payload

    plaintext = aead_decrypt(ciphertext, tag, key, nonce, ad)
    assert plaintext == payload


def test_security_aead_decrypt_invalid_tag() -> None:
    from mcubridge.security.security import aead_encrypt, aead_decrypt

    key = b"C" * 32
    nonce = b"\x03" * 12
    ad = b"aad"

    ciphertext, tag = aead_encrypt(b"data", key, nonce, ad)
    wrong_tag = bytes(b ^ 0xFF for b in tag)

    with pytest.raises(ValueError, match="AEAD decryption failed"):
        aead_decrypt(ciphertext, wrong_tag, key, nonce, ad)


def test_extract_nonce_counter_wrong_size() -> None:
    with pytest.raises(ValueError, match="Nonce must be"):
        extract_nonce_counter(b"\x00" * 11)


def test_validate_nonce_counter_wrong_size_nonce() -> None:
    ok, counter = validate_nonce_counter(b"\x00" * 5, 0)
    assert ok is False
    assert counter == 0


def test_verify_crypto_integrity_sha_failure() -> None:
    import hashlib
    from unittest.mock import patch

    with patch.object(hashlib, "sha256") as mock_sha:
        mock_sha.return_value.hexdigest.return_value = "wrong_hash"
        assert verify_crypto_integrity() is False


def test_dec_hook_bytes_branch() -> None:
    from mcubridge.config.settings import _dec_hook

    result = _dec_hook(bytes, "  hello  ")
    assert result == b"hello"


def test_dec_hook_tuple_branch() -> None:
    from mcubridge.config.settings import _dec_hook

    result = _dec_hook(tuple, "word1 word2 word3")
    assert result == ("word1", "word2", "word3")


def test_dec_hook_str_path_branch() -> None:
    from mcubridge.config.settings import _dec_hook

    result = _dec_hook(str, "/some/path/to/file")
    assert "/" in result


def test_dec_hook_str_empty_returns_none() -> None:
    from mcubridge.config.settings import _dec_hook

    result = _dec_hook(str, "")
    assert result is None


def test_dec_hook_type_error() -> None:
    from mcubridge.config.settings import _dec_hook

    with pytest.raises(TypeError):
        _dec_hook(int, "not_convertible")


def test_load_runtime_config_with_overrides() -> None:
    from mcubridge.config.settings import load_runtime_config

    config = load_runtime_config(overrides={"serial_baud": 115200, "serial_shared_secret": b"override_key"})
    assert config.serial_baud == 115200
    assert config.serial_shared_secret == b"override_key"


def test_frame_build_parse_encrypted() -> None:
    from mcubridge.protocol.frame import build_frame, parse_frame
    from mcubridge.protocol.protocol import Command

    key = b"D" * 32
    nonce = b"\x04" * 12

    body = build_frame(
        command_id=Command.CMD_SET_PIN_MODE.value,
        sequence_id=42,
        payload=b"encrypted_test",
        nonce=nonce,
        session_key=key,
    )
    envelope = parse_frame(body, session_key=key)
    assert envelope.payload == b"encrypted_test"
    assert (envelope.command_id & ~protocol.CMD_FLAG_COMPRESSED) == Command.CMD_SET_PIN_MODE.value


def test_frame_build_rle_compressed() -> None:
    from mcubridge.protocol.frame import build_frame, parse_frame
    from mcubridge.protocol.protocol import Command, CMD_FLAG_COMPRESSED

    body = build_frame(
        command_id=Command.CMD_SET_PIN_MODE.value,
        sequence_id=1,
        payload=b"\xAA" * 50,
        nonce=b"\x00" * 12,
        session_key=None,
    )
    envelope = parse_frame(body, session_key=None)
    assert envelope.payload == b"\xAA" * 50


def test_frame_parse_wrong_key_raises() -> None:
    from mcubridge.protocol.frame import build_frame, parse_frame
    from mcubridge.protocol.protocol import Command

    key1 = b"E" * 32
    key2 = b"F" * 32
    nonce = b"\x05" * 12

    body = build_frame(
        command_id=Command.CMD_SET_PIN_MODE.value,
        sequence_id=1,
        payload=b"secret",
        nonce=nonce,
        session_key=key1,
    )
    with pytest.raises((ValueError, Exception)):
        parse_frame(body, session_key=key2)
