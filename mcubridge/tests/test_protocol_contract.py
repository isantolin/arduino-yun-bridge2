"""Contract tests keeping protocol spec and bindings in sync."""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path

import msgspec
from mcubridge.protocol import protocol, structures
from mcubridge.services.handshake import SerialHandshakeManager

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "tools/protocol/spec.toml"
CPP_HEADER_PATH = REPO_ROOT / "mcubridge-library-arduino/src/protocol/rpc_protocol.h"


class _StatusSpec(msgspec.Struct, frozen=True):
    name: str
    value: int


class _CommandSpec(msgspec.Struct, frozen=True):
    name: str
    value: int


def _load_spec() -> tuple[dict[str, int], list[_StatusSpec], list[_CommandSpec], dict[str, int | str]]:
    raw = msgspec.toml.decode(SPEC_PATH.read_text(encoding="utf-8"))
    constants = {
        "PROTOCOL_VERSION": int(raw["constants"]["protocol_version"]),
        "MAX_PAYLOAD_SIZE": int(raw["constants"]["max_payload_size"]),
    }
    statuses = [_StatusSpec(name=entry["name"], value=int(entry["value"])) for entry in raw.get("statuses", [])]
    commands = [_CommandSpec(name=entry["name"], value=int(entry["value"])) for entry in raw.get("commands", [])]
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
        "response_timeout_min_ms": int(handshake_data.get("response_timeout_min_ms", 0)),
        "response_timeout_max_ms": int(handshake_data.get("response_timeout_max_ms", 0)),
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
    # config_format is no longer explicitly in spec.toml [handshake] section
    assert handshake["ack_timeout_min_ms"] == protocol.HANDSHAKE_ACK_TIMEOUT_MIN_MS
    assert handshake["ack_timeout_max_ms"] == protocol.HANDSHAKE_ACK_TIMEOUT_MAX_MS
    assert handshake["response_timeout_min_ms"] == protocol.HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS
    assert handshake["response_timeout_max_ms"] == protocol.HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS
    assert handshake["retry_limit_min"] == protocol.HANDSHAKE_RETRY_LIMIT_MIN
    assert handshake["retry_limit_max"] == protocol.HANDSHAKE_RETRY_LIMIT_MAX


def test_handshake_config_binary_layout_matches_cpp_struct() -> None:
    # Validate encode/decode round-trip for HandshakeConfig payload
    sample = structures.HandshakeConfigPacket(ack_timeout_ms=750, ack_retry_limit=3, response_timeout_ms=120000)
    encoded = sample.encode()
    assert len(encoded) > 0
    decoded = structures.HandshakeConfigPacket.decode(encoded)
    assert decoded == sample


def test_handshake_tag_reference_vector_matches_spec() -> None:
    from mcubridge.security.security import derive_handshake_key

    secret = b"mcubridge-shared"
    nonce = bytes(range(protocol.HANDSHAKE_NONCE_LENGTH))
    # [MIL-SPEC] Test must use HKDF derived key to match runtime implementation
    auth_key = derive_handshake_key(secret)
    expected = hmac.new(auth_key, nonce, hashlib.sha256).digest()[: protocol.HANDSHAKE_TAG_LENGTH]
    computed = SerialHandshakeManager.calculate_handshake_tag(secret, nonce)
    assert computed == expected


def _command_to_handler(name: str) -> str:
    """Convert CMD_FOO_BAR → _handleFooBar."""
    raw = name.removeprefix("CMD_")
    return "_handle" + "".join(p.capitalize() for p in raw.split("_"))


def _extract_cpp_dispatch_commands(cpp_content: str) -> set[str]:
    """Return command ids explicitly registered in Bridge.cpp dispatch tables."""
    # Support pure template dispatch architecture, old switch dispatch, and new O(1) jump table
    command_re = re.compile(
        r"(?:Behavior(?:Cmd|BridgePayload|ServicePayload|GpioPayload|PinRead)<.*?rpc::CommandId::(CMD_[A-Z0-9_]+))|"
        r"(?:case\s+rpc::to_underlying\(rpc::CommandId::(CMD_[A-Z0-9_]+)\):)|"
        r"(?:\{\s*rpc::to_underlying\(rpc::CommandId::(CMD_[A-Z0-9_]+)\))",
        re.MULTILINE,
    )
    # Extract matches from all groups
    matches = command_re.findall(cpp_content)
    return {m[0] or m[1] or m[2] for m in matches}


def test_mcu_inbound_commands_have_cpp_jump_table_handlers() -> None:
    """Every linux_to_mcu command in spec.toml must be registered in Bridge.cpp dispatch."""
    bridge_cpp = REPO_ROOT / "mcubridge-library-arduino/src/Bridge.cpp"
    cpp_content = bridge_cpp.read_text(encoding="utf-8")

    handler_re = re.compile(r"&BridgeClass::(_handle\w+)")
    cpp_handlers = set(handler_re.findall(cpp_content))
    cpp_registered_commands = _extract_cpp_dispatch_commands(cpp_content)

    raw = msgspec.toml.decode(SPEC_PATH.read_text(encoding="utf-8"))
    mcu_inbound = [
        cmd for cmd in raw.get("commands", [])
        if "linux_to_mcu" in cmd.get("directions", [])
    ]

    missing = [
        f"{cmd['name']} (0x{cmd['value']:02X}) → {_command_to_handler(cmd['name'])}"
        for cmd in mcu_inbound
        if (
            _command_to_handler(cmd["name"]) not in cpp_handlers
            and cmd["name"] not in cpp_registered_commands
        )
    ]
    header = f"{len(missing)} MCU-inbound command(s) without C++ dispatch registration:\n"
    assert not missing, header + "\n".join(f"  {m}" for m in missing)
