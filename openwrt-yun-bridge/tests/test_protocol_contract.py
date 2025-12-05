"""Contract tests keeping protocol spec and bindings in sync."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomllib

from yunbridge import const
from yunbridge.rpc import protocol as rpc_protocol

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "tools/protocol/spec.toml"
CPP_HEADER_PATH = (
    REPO_ROOT / "openwrt-library-arduino/src/protocol/rpc_protocol.h"
)


@dataclass(frozen=True)
class _StatusSpec:
    name: str
    value: int


@dataclass(frozen=True)
class _CommandSpec:
    name: str
    value: int


def _load_spec() -> tuple[
    dict[str, int], list[_StatusSpec], list[_CommandSpec], dict[str, object]
]:
    raw = tomllib.loads(SPEC_PATH.read_text(encoding="utf-8"))
    constants = {
        "PROTOCOL_VERSION": int(raw["constants"]["protocol_version"]),
        "MAX_PAYLOAD_SIZE": int(raw["constants"]["max_payload_size"]),
        "RPC_BUFFER_SIZE": int(raw["constants"]["rpc_buffer_size"]),
    }
    statuses = [
        _StatusSpec(name=entry["name"], value=int(entry["value"]))
        for entry in raw.get("statuses", [])
    ]
    commands = [
        _CommandSpec(name=entry["name"], value=int(entry["value"]))
        for entry in raw.get("commands", [])
    ]
    handshake = {
        "nonce_length": int(raw.get("handshake", {}).get("nonce_length", 0)),
        "tag_length": int(raw.get("handshake", {}).get("tag_length", 0)),
        "tag_algorithm": raw.get("handshake", {}).get("tag_algorithm", ""),
    }
    return constants, statuses, commands, handshake


def test_protocol_spec_matches_generated_bindings() -> None:
    constants, statuses, commands, handshake = _load_spec()
    header_text = CPP_HEADER_PATH.read_text(encoding="utf-8")

    assert rpc_protocol.PROTOCOL_VERSION == constants["PROTOCOL_VERSION"]
    assert rpc_protocol.MAX_PAYLOAD_SIZE == constants["MAX_PAYLOAD_SIZE"]
    assert rpc_protocol.RPC_BUFFER_SIZE == constants["RPC_BUFFER_SIZE"]

    for status in statuses:
        enum_member = rpc_protocol.Status[status.name]
        assert enum_member.value == status.value
        expected_define = f"#define STATUS_{status.name} 0x{status.value:02X}"
        assert expected_define in header_text

    for command in commands:
        enum_member = rpc_protocol.Command[command.name]
        assert enum_member.value == command.value
        expected_define = f"#define {command.name} 0x{command.value:02X}"
        assert expected_define in header_text

    assert handshake["nonce_length"] == const.SERIAL_NONCE_LENGTH
    assert handshake["tag_length"] == const.SERIAL_HANDSHAKE_TAG_LEN
    assert handshake["tag_algorithm"] == "HMAC-SHA256"
