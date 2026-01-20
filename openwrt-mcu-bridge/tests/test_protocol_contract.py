"""Contract tests keeping protocol spec and bindings in sync."""

from __future__ import annotations

import hashlib
import hmac
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import tomllib

from mcubridge.rpc import protocol
from mcubridge.services.handshake import SerialHandshakeManager

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "tools/protocol/spec.toml"
CPP_HEADER_PATH = REPO_ROOT / "openwrt-library-arduino/src/protocol/protocol.h"


@dataclass(frozen=True)
class _StatusSpec:
    name: str
    value: int


@dataclass(frozen=True)
class _CommandSpec:
    name: str
    value: int


def _load_spec() -> (
    tuple[dict[str, int], list[_StatusSpec], list[_CommandSpec], dict[str, object]]
):
    raw = tomllib.loads(SPEC_PATH.read_text(encoding="utf-8"))
    constants = {
        "PROTOCOL_VERSION": int(raw["constants"]["protocol_version"]),
        "MAX_PAYLOAD_SIZE": int(raw["constants"]["max_payload_size"]),
    }
    statuses = [
        _StatusSpec(name=entry["name"], value=int(entry["value"]))
        for entry in raw.get("statuses", [])
    ]
    commands = [
        _CommandSpec(name=entry["name"], value=int(entry["value"]))
        for entry in raw.get("commands", [])
    ]
    handshake_data = raw.get("handshake", {})
    handshake = {
        "nonce_length": int(handshake_data.get("nonce_length", 0)),
        "tag_length": int(handshake_data.get("tag_length", 0)),
        "tag_algorithm": handshake_data.get("tag_algorithm", ""),
        "tag_description": handshake_data.get("tag_description", ""),
        "config_format": handshake_data.get("config_format", ""),
        "config_description": handshake_data.get("config_description", ""),
        "ack_timeout_min_ms": int(handshake_data.get("ack_timeout_min_ms", 0)),
        "ack_timeout_max_ms": int(handshake_data.get("ack_timeout_max_ms", 0)),
        "response_timeout_min_ms": int(
            handshake_data.get("response_timeout_min_ms", 0)
        ),
        "response_timeout_max_ms": int(
            handshake_data.get("response_timeout_max_ms", 0)
        ),
        "retry_limit_min": int(handshake_data.get("retry_limit_min", 0)),
        "retry_limit_max": int(handshake_data.get("retry_limit_max", 0)),
    }
    return constants, statuses, commands, handshake


def test_protocol_spec_matches_generated_bindings() -> None:
    constants, statuses, commands, handshake = _load_spec()
    header_text = CPP_HEADER_PATH.read_text(encoding="utf-8")

    assert protocol.PROTOCOL_VERSION == constants["PROTOCOL_VERSION"]
    assert protocol.MAX_PAYLOAD_SIZE == constants["MAX_PAYLOAD_SIZE"]

    for status in statuses:
        enum_member = protocol.Status[status.name]
        assert enum_member.value == status.value
        # Check for enum class entry: STATUS_NAME = VALUE,
        enum_entry = f"STATUS_{status.name} = {status.value},"
        assert enum_entry in header_text

    for command in commands:
        enum_member = protocol.Command[command.name]
        assert enum_member.value == command.value
        # Check for enum class entry: NAME = VALUE,
        enum_entry = f"{command.name} = {command.value},"
        assert enum_entry in header_text

    assert handshake["nonce_length"] == protocol.HANDSHAKE_NONCE_LENGTH
    assert handshake["tag_length"] == protocol.HANDSHAKE_TAG_LENGTH
    assert handshake["tag_algorithm"] == protocol.HANDSHAKE_TAG_ALGORITHM
    assert handshake["tag_description"] == protocol.HANDSHAKE_TAG_DESCRIPTION
    assert handshake["config_format"] == protocol.HANDSHAKE_CONFIG_FORMAT
    assert handshake["ack_timeout_min_ms"] == protocol.HANDSHAKE_ACK_TIMEOUT_MIN_MS
    assert handshake["ack_timeout_max_ms"] == protocol.HANDSHAKE_ACK_TIMEOUT_MAX_MS
    assert handshake["response_timeout_min_ms"] == protocol.HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS
    assert handshake["response_timeout_max_ms"] == protocol.HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS
    assert handshake["retry_limit_min"] == protocol.HANDSHAKE_RETRY_LIMIT_MIN
    assert handshake["retry_limit_max"] == protocol.HANDSHAKE_RETRY_LIMIT_MAX


def test_handshake_config_binary_layout_matches_cpp_struct() -> None:
    _, _, _, handshake = _load_spec()
    fmt = handshake["config_format"]
    assert fmt, "Handshake config format missing in spec"

    packed_size = struct.calcsize(fmt)
    assert packed_size == protocol.HANDSHAKE_CONFIG_SIZE

    header_text = CPP_HEADER_PATH.read_text(encoding="utf-8")
    match = re.search(r"RPC_HANDSHAKE_CONFIG_SIZE\s*=\s*(\d+)u?", header_text)
    assert match, "RPC_HANDSHAKE_CONFIG_SIZE missing in header"
    assert int(match.group(1)) == packed_size

    sample_payload = struct.pack(fmt, 750, 3, 120000)
    assert len(sample_payload) == packed_size


def test_handshake_tag_reference_vector_matches_spec() -> None:
    secret = b"mcubridge-shared"
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    expected = hmac.new(secret, nonce, hashlib.sha256).digest()[
        : protocol.HANDSHAKE_TAG_LENGTH
    ]
    computed = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    assert computed == expected
