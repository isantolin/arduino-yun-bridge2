#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Jinja2: Declarative templates for C++ and Python outputs.

Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from jinja2 import Environment, FileSystemLoader

# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY VALIDATION (CRITICAL)
# ═════════════════════════════════════════════════════════════════════════════
REQUIRED_DEPS = ["msgspec", "typer", "jinja2"]
MISSING_DEPS = [dep for dep in REQUIRED_DEPS if importlib.util.find_spec(dep) is None]

if MISSING_DEPS:
    sys.stderr.write("\n" + "!" * 80 + "\n")
    sys.stderr.write("ERROR: Missing Python dependencies required for protocol generation:\n")
    for dep in MISSING_DEPS:
        sys.stderr.write(f"  - {dep}\n")
    sys.stderr.write("\nTo fix this, run:\n")
    sys.stderr.write(f"  pip install {' '.join(MISSING_DEPS)}\n")
    sys.stderr.write("!" * 80 + "\n\n")
    sys.exit(1)
# ═════════════════════════════════════════════════════════════════════════════

# Load ProtocolSpec directly from spec_model.py via importlib.util
if TYPE_CHECKING:
    from mcubridge.protocol.spec_model import ProtocolSpec
else:
    _SPEC_MODEL_PATH = (
        Path(__file__).resolve().parent.parent.parent
        / "mcubridge" / "mcubridge" / "protocol" / "spec_model.py"
    )
    _loader_spec = importlib.util.spec_from_file_location("spec_model", str(_SPEC_MODEL_PATH))
    assert _loader_spec is not None and _loader_spec.loader is not None
    _spec_mod = importlib.util.module_from_spec(_loader_spec)
    _loader_spec.loader.exec_module(_spec_mod)
    ProtocolSpec = _spec_mod.ProtocolSpec

app = typer.Typer(help="Protocol binding generator for MCU Bridge v2.")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = Path(__file__).parent / "templates"
VERSION_PATH = REPO_ROOT / "VERSION"

# ═════════════════════════════════════════════════════════════════════════════
# PROTO PARSER — extract message definitions from .proto files
# ═════════════════════════════════════════════════════════════════════════════

# Messages that do NOT get a Packet class (used directly or handled specially)
PACKET_EXCLUDE: frozenset[str] = frozenset({"RpcContainer", "LinkSync", "Capabilities"})

# Proto3 scalar → Python type annotation string
PROTO_PYTHON_TYPE_MAP: dict[str, str] = {
    "uint32": "Annotated[int, msgspec.Meta(ge=0)]",
    "int32": "int",
    "string": "str",
    "bytes": "bytes",
    "bool": "bool",
}


@dataclasses.dataclass(frozen=True)
class ProtoField:
    name: str
    proto_type: str


@dataclasses.dataclass(frozen=True)
class ProtoMessage:
    name: str
    fields: tuple[ProtoField, ...]


def parse_proto_messages(proto_path: Path) -> list[ProtoMessage]:
    """Parse message definitions from a .proto file."""
    content = proto_path.read_text(encoding="utf-8")
    messages: list[ProtoMessage] = []

    for match in re.finditer(r"message\s+(\w+)\s*\{([^}]*)}", content):
        name = match.group(1)
        body = match.group(2)
        fields: list[ProtoField] = []
        for field_match in re.finditer(r"^\s*(\w+)\s+(\w+)\s*=\s*\d+", body, re.MULTILINE):
            fields.append(ProtoField(
                proto_type=field_match.group(1),
                name=field_match.group(2),
            ))
        messages.append(ProtoMessage(name=name, fields=tuple(fields)))

    return messages


def packet_class_name(proto_name: str) -> str:
    """Convert proto message name to Python Packet class name."""
    if proto_name.endswith("Packet"):
        return proto_name
    return f"{proto_name}Packet"


class JinjaGenerator:
    def __init__(self) -> None:
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            keep_trailing_newline=True,
        )
        self.env.filters["cpp_digits"] = self._cpp_digit_separator

    @staticmethod
    def _cpp_digit_separator(value: object) -> str:
        """Format integers >= 10'000 with C++14 digit separators."""
        if not isinstance(value, int) or abs(value) < 10_000:
            return str(value)
        s = str(abs(value))
        parts: list[str] = []
        while s:
            parts.append(s[-3:])
            s = s[:-3]
        result = "'".join(reversed(parts))
        return f"-{result}" if value < 0 else result

    def generate_cpp_header(self, spec: ProtocolSpec, out_path: Path, version: str) -> None:
        template = self.env.get_template("rpc_protocol.h.j2")

        v_major, v_minor, v_patch = map(int, version.split("."))

        c = spec.constants
        constants = [
            {"name": "PROTOCOL_VERSION", "type": "uint8_t", "value": c["protocol_version"]},
            {"name": "FIRMWARE_VERSION_MAJOR", "type": "uint8_t", "value": v_major},
            {"name": "FIRMWARE_VERSION_MINOR", "type": "uint8_t", "value": v_minor},
            {"name": "FIRMWARE_VERSION_PATCH", "type": "uint8_t", "value": v_patch},
            {"name": "RPC_DEFAULT_BAUDRATE", "type": "unsigned long", "value": c["default_baudrate"]},
            {"name": "MAX_PAYLOAD_SIZE", "type": "size_t", "value": c["max_payload_size"]},
            {
                "name": "RPC_DEFAULT_SAFE_BAUDRATE",
                "type": "unsigned long",
                "value": c["default_safe_baudrate"],
            },
            {"name": "RPC_SERIAL_TIMEOUT_MS", "type": "uint32_t", "value": spec.hardware["serial_timeout_ms"]},
            {"name": "RPC_SPI_TIMEOUT_MS", "type": "uint32_t", "value": spec.hardware["spi_timeout_ms"]},
            {"name": "RPC_MAX_FILEPATH_LENGTH", "type": "size_t", "value": c["max_filepath_length"]},
            {
                "name": "RPC_MAX_DATASTORE_KEY_LENGTH",
                "type": "size_t",
                "value": c["max_datastore_key_length"],
            },
            {
                "name": "RPC_DEFAULT_ACK_TIMEOUT_MS",
                "type": "unsigned int",
                "value": c["default_ack_timeout_ms"],
            },
            {"name": "RPC_DEFAULT_RETRY_LIMIT", "type": "uint8_t", "value": c["default_retry_limit"]},
            {"name": "RPC_MAX_PENDING_TX_FRAMES", "type": "uint8_t", "value": c["max_pending_tx_frames"]},
            {"name": "RPC_INVALID_ID_SENTINEL", "type": "uint16_t", "value": c["invalid_id_sentinel"]},
            {"name": "RPC_NULL_TERMINATOR", "type": "char", "value": c["rpc_null_terminator"]},
            {"name": "RPC_COMMAND_STRIDE", "type": "uint8_t", "value": c["rpc_command_stride"]},
            {"name": "RPC_COMMAND_GROUP_SHIFT", "type": "uint8_t", "value": c["rpc_command_group_shift"]},
            {"name": "RPC_COMMAND_GROUP_OFFSET", "type": "uint8_t", "value": c["rpc_command_group_offset"]},
            {"name": "RPC_TIMER_OVERFLOW_THRESHOLD", "type": "uint32_t", "value": c["rpc_timer_overflow_threshold"]},
            {"name": "RPC_CMD_FLAG_COMPRESSED", "type": "uint16_t", "value": c["cmd_flag_compressed"]},
            {"name": "RPC_CMD_FLAG_COMPRESSED_BIT", "type": "uint8_t", "value": c["rpc_cmd_flag_compressed_bit"]},
            {"name": "RPC_UINT8_MASK", "type": "uint8_t", "value": c["uint8_mask"]},
            {"name": "RPC_UINT16_MAX", "type": "uint16_t", "value": c["uint16_max"]},
            {"name": "RPC_BOOTLOADER_MAGIC", "type": "uint32_t", "value": c["bootloader_magic"]},
            {
                "name": "RPC_PROCESS_DEFAULT_EXIT_CODE",
                "type": "uint8_t",
                "value": c["process_default_exit_code"],
            },
            {"name": "RPC_CRC32_MASK", "type": "uint32_t", "value": c["crc32_mask"]},
            {"name": "RPC_CRC_INITIAL", "type": "uint32_t", "value": c["crc_initial"]},
            {"name": "RPC_CRC_POLYNOMIAL", "type": "uint32_t", "value": c["crc_polynomial"]},
            {"name": "RPC_FRAME_DELIMITER", "type": "uint8_t", "value": c["frame_delimiter"]},
            {"name": "RPC_DIGITAL_LOW", "type": "uint8_t", "value": c["digital_low"]},
            {"name": "RPC_DIGITAL_HIGH", "type": "uint8_t", "value": c["digital_high"]},
            {"name": "RPC_RLE_ESCAPE_BYTE", "type": "uint8_t", "value": c["rle_escape_byte"]},
            {"name": "RPC_RLE_MIN_RUN_LENGTH", "type": "uint8_t", "value": c["rle_min_run_length"]},
            {"name": "RPC_RLE_MAX_RUN_LENGTH", "type": "uint16_t", "value": c["rle_max_run_length"]},
            {
                "name": "RPC_RLE_SINGLE_ESCAPE_MARKER",
                "type": "uint8_t",
                "value": c["rle_single_escape_marker"],
            },
            {"name": "RPC_RLE_EXPANSION_FACTOR", "type": "uint8_t", "value": c["rle_expansion_factor"]},
            {"name": "RPC_RLE_OFFSET", "type": "uint8_t", "value": c["rle_offset"]},
            {"name": "RPC_RLE_MIN_COMPRESS_INPUT_SIZE", "type": "size_t", "value": c["rle_min_compress_input_size"]},
            {"name": "RPC_RLE_MIN_COMPRESS_SAVINGS", "type": "size_t", "value": c["rle_min_compress_savings"]},
            {"name": "RPC_SHA256_DIGEST_SIZE", "type": "uint8_t", "value": spec.hardware["sha256_digest_size"]},
            {"name": "RPC_SHA256_KAT_BUFFER_SIZE", "type": "uint8_t", "value": spec.hardware["sha256_kat_buffer_size"]},
            {"name": "RPC_STATUS_CODE_MIN", "type": "uint8_t", "value": c["status_code_min"]},
            {"name": "RPC_STATUS_CODE_MAX", "type": "uint8_t", "value": c["status_code_max"]},
            {"name": "RPC_SYSTEM_COMMAND_MIN", "type": "uint16_t", "value": c["system_command_min"]},
            {"name": "RPC_SYSTEM_COMMAND_MAX", "type": "uint16_t", "value": c["system_command_max"]},
            {"name": "RPC_GPIO_COMMAND_MIN", "type": "uint16_t", "value": c["gpio_command_min"]},
            {"name": "RPC_GPIO_COMMAND_MAX", "type": "uint16_t", "value": c["gpio_command_max"]},
            {"name": "RPC_CONSOLE_COMMAND_MIN", "type": "uint16_t", "value": c["console_command_min"]},
            {"name": "RPC_CONSOLE_COMMAND_MAX", "type": "uint16_t", "value": c["console_command_max"]},
            {"name": "RPC_DATASTORE_COMMAND_MIN", "type": "uint16_t", "value": c["datastore_command_min"]},
            {"name": "RPC_DATASTORE_COMMAND_MAX", "type": "uint16_t", "value": c["datastore_command_max"]},
            {"name": "RPC_MAILBOX_COMMAND_MIN", "type": "uint16_t", "value": c["mailbox_command_min"]},
            {"name": "RPC_MAILBOX_COMMAND_MAX", "type": "uint16_t", "value": c["mailbox_command_max"]},
            {
                "name": "RPC_FILESYSTEM_COMMAND_MIN",
                "type": "uint16_t",
                "value": c["filesystem_command_min"],
            },
            {
                "name": "RPC_FILESYSTEM_COMMAND_MAX",
                "type": "uint16_t",
                "value": c["filesystem_command_max"],
            },
            {"name": "RPC_PROCESS_COMMAND_MIN", "type": "uint16_t", "value": c["process_command_min"]},
            {"name": "RPC_PROCESS_COMMAND_MAX", "type": "uint16_t", "value": c["process_command_max"]},
            {"name": "RPC_SPI_COMMAND_MIN", "type": "uint16_t", "value": c["spi_command_min"]},
            {"name": "RPC_SPI_COMMAND_MAX", "type": "uint16_t", "value": c["spi_command_max"]},
        ]

        hs = spec.handshake
        handshake_constants = [
            {"name": "RPC_HANDSHAKE_NONCE_LENGTH", "type": "unsigned int", "value": hs["nonce_length"]},
            {"name": "RPC_HANDSHAKE_TAG_LENGTH", "type": "unsigned int", "value": hs["tag_length"]},
            {
                "name": "RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS",
                "type": "uint32_t",
                "value": hs["ack_timeout_min_ms"],
            },
            {
                "name": "RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS",
                "type": "uint32_t",
                "value": hs["ack_timeout_max_ms"],
            },
            {
                "name": "RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS",
                "type": "uint32_t",
                "value": hs["response_timeout_min_ms"],
            },
            {
                "name": "RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS",
                "type": "uint32_t",
                "value": hs["response_timeout_max_ms"],
            },
            {"name": "RPC_HANDSHAKE_RETRY_LIMIT_MIN", "type": "unsigned int", "value": hs["retry_limit_min"]},
            {"name": "RPC_HANDSHAKE_RETRY_LIMIT_MAX", "type": "unsigned int", "value": hs["retry_limit_max"]},
            {
                "name": "RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH",
                "type": "unsigned int",
                "value": hs["hkdf_output_length"],
            },
            {
                "name": "RPC_HANDSHAKE_NONCE_RANDOM_BYTES",
                "type": "unsigned int",
                "value": hs["nonce_random_bytes"],
            },
            {
                "name": "RPC_HANDSHAKE_NONCE_COUNTER_BYTES",
                "type": "unsigned int",
                "value": hs["nonce_counter_bytes"],
            },
        ]

        handshake_data = {
            "hkdf_salt": hs["hkdf_salt"],
            "hkdf_salt_bytes": ", ".join(f"0x{ord(c):02X}" for c in hs["hkdf_salt"]),
            "hkdf_salt_len": len(hs["hkdf_salt"]),
            "hkdf_info_auth": hs["hkdf_info_auth"],
            "hkdf_info_auth_bytes": ", ".join(f"0x{ord(c):02X}" for c in hs["hkdf_info_auth"]),
            "hkdf_info_auth_len": len(hs["hkdf_info_auth"]),
        }

        render = template.render(
            constants=constants,
            handshake_constants=handshake_constants,
            handshake=handshake_data,
            capabilities=spec.capabilities,
            architectures=spec.architectures,
            statuses=spec.statuses,
            commands=spec.commands,
            ack_commands=[c for c in spec.commands if c.requires_ack],
        )
        out_path.write_text(render, encoding="utf-8")

    def generate_cpp_structs(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("rpc_structs.h.j2")
        render = template.render()
        out_path.write_text(render, encoding="utf-8")

    def generate_cpp_hw_config(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("rpc_hw_config.h.j2")
        render = template.render(hardware=spec.hardware)
        out_path.write_text(render, encoding="utf-8")

    def generate_python(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("protocol.py.j2")

        c = spec.constants
        constants = [
            {"name": "PROTOCOL_VERSION", "type": "int", "value": c["protocol_version"]},
            {"name": "DEFAULT_BAUDRATE", "type": "int", "value": c["default_baudrate"]},
            {"name": "MAX_PAYLOAD_SIZE", "type": "int", "value": c["max_payload_size"]},
            {"name": "DEFAULT_SAFE_BAUDRATE", "type": "int", "value": c["default_safe_baudrate"]},
            {"name": "SERIAL_TIMEOUT_MS", "type": "int", "value": spec.hardware["serial_timeout_ms"]},
            {"name": "SPI_TIMEOUT_MS", "type": "int", "value": spec.hardware["spi_timeout_ms"]},
            {"name": "MAX_FILEPATH_LENGTH", "type": "int", "value": c["max_filepath_length"]},
            {"name": "MAX_DATASTORE_KEY_LENGTH", "type": "int", "value": c["max_datastore_key_length"]},
            {"name": "DEFAULT_ACK_TIMEOUT_MS", "type": "int", "value": c["default_ack_timeout_ms"]},
            {"name": "DEFAULT_RETRY_LIMIT", "type": "int", "value": c["default_retry_limit"]},
            {"name": "MAX_PENDING_TX_FRAMES", "type": "int", "value": c["max_pending_tx_frames"]},
            {"name": "INVALID_ID_SENTINEL", "type": "int", "value": c["invalid_id_sentinel"]},
            {"name": "NULL_TERMINATOR", "type": "int", "value": c["rpc_null_terminator"]},
            {"name": "COMMAND_STRIDE", "type": "int", "value": c["rpc_command_stride"]},
            {"name": "COMMAND_GROUP_SHIFT", "type": "int", "value": c["rpc_command_group_shift"]},
            {"name": "COMMAND_GROUP_OFFSET", "type": "int", "value": c["rpc_command_group_offset"]},
            {"name": "TIMER_OVERFLOW_THRESHOLD", "type": "int", "value": c["rpc_timer_overflow_threshold"]},
            {"name": "CMD_FLAG_COMPRESSED", "type": "int", "value": c["cmd_flag_compressed"]},
            {"name": "CMD_FLAG_COMPRESSED_BIT", "type": "int", "value": c["rpc_cmd_flag_compressed_bit"]},
            {"name": "UINT8_MASK", "type": "int", "value": c["uint8_mask"]},
            {"name": "UINT16_MAX", "type": "int", "value": c["uint16_max"]},
            {"name": "BOOTLOADER_MAGIC", "type": "int", "value": c["bootloader_magic"]},
            {"name": "PROCESS_DEFAULT_EXIT_CODE", "type": "int", "value": c["process_default_exit_code"]},
            {"name": "CRC32_MASK", "type": "int", "value": c["crc32_mask"]},
            {"name": "CRC_INITIAL", "type": "int", "value": c["crc_initial"]},
            {"name": "CRC_POLYNOMIAL", "type": "int", "value": c["crc_polynomial"]},
            {"name": "FRAME_DELIMITER", "type": "bytes", "value": f"bytes([{c['frame_delimiter']}])"},
            {"name": "DIGITAL_LOW", "type": "int", "value": c["digital_low"]},
            {"name": "DIGITAL_HIGH", "type": "int", "value": c["digital_high"]},
            {"name": "RLE_ESCAPE_BYTE", "type": "int", "value": c["rle_escape_byte"]},
            {"name": "RLE_MIN_RUN_LENGTH", "type": "int", "value": c["rle_min_run_length"]},
            {"name": "RLE_MAX_RUN_LENGTH", "type": "int", "value": c["rle_max_run_length"]},
            {"name": "RLE_SINGLE_ESCAPE_MARKER", "type": "int", "value": c["rle_single_escape_marker"]},
            {"name": "RLE_EXPANSION_FACTOR", "type": "int", "value": c["rle_expansion_factor"]},
            {"name": "RLE_OFFSET", "type": "int", "value": c["rle_offset"]},
            {"name": "RLE_MIN_COMPRESS_INPUT_SIZE", "type": "int", "value": c["rle_min_compress_input_size"]},
            {"name": "RLE_MIN_COMPRESS_SAVINGS", "type": "int", "value": c["rle_min_compress_savings"]},
            {"name": "SHA256_DIGEST_SIZE", "type": "int", "value": spec.hardware["sha256_digest_size"]},
            {"name": "SHA256_KAT_BUFFER_SIZE", "type": "int", "value": spec.hardware["sha256_kat_buffer_size"]},
            {"name": "STATUS_CODE_MIN", "type": "int", "value": c["status_code_min"]},
            {"name": "STATUS_CODE_MAX", "type": "int", "value": c["status_code_max"]},
            {"name": "SYSTEM_COMMAND_MIN", "type": "int", "value": c["system_command_min"]},
            {"name": "SYSTEM_COMMAND_MAX", "type": "int", "value": c["system_command_max"]},
            {"name": "GPIO_COMMAND_MIN", "type": "int", "value": c["gpio_command_min"]},
            {"name": "GPIO_COMMAND_MAX", "type": "int", "value": c["gpio_command_max"]},
            {"name": "CONSOLE_COMMAND_MIN", "type": "int", "value": c["console_command_min"]},
            {"name": "CONSOLE_COMMAND_MAX", "type": "int", "value": c["console_command_max"]},
            {"name": "DATASTORE_COMMAND_MIN", "type": "int", "value": c["datastore_command_min"]},
            {"name": "DATASTORE_COMMAND_MAX", "type": "int", "value": c["datastore_command_max"]},
            {"name": "MAILBOX_COMMAND_MIN", "type": "int", "value": c["mailbox_command_min"]},
            {"name": "MAILBOX_COMMAND_MAX", "type": "int", "value": c["mailbox_command_max"]},
            {"name": "FILESYSTEM_COMMAND_MIN", "type": "int", "value": c["filesystem_command_min"]},
            {"name": "FILESYSTEM_COMMAND_MAX", "type": "int", "value": c["filesystem_command_max"]},
            {"name": "PROCESS_COMMAND_MIN", "type": "int", "value": c["process_command_min"]},
            {"name": "PROCESS_COMMAND_MAX", "type": "int", "value": c["process_command_max"]},
            {"name": "SPI_COMMAND_MIN", "type": "int", "value": c["spi_command_min"]},
            {"name": "SPI_COMMAND_MAX", "type": "int", "value": c["spi_command_max"]},
        ]

        hs = spec.handshake
        handshake_constants = [
            {"name": "HANDSHAKE_NONCE_LENGTH", "type": "int", "value": hs["nonce_length"]},
            {"name": "HANDSHAKE_TAG_LENGTH", "type": "int", "value": hs["tag_length"]},
            {
                "name": "HANDSHAKE_ACK_TIMEOUT_MIN_MS",
                "type": "int",
                "value": hs["ack_timeout_min_ms"],
            },
            {
                "name": "HANDSHAKE_ACK_TIMEOUT_MAX_MS",
                "type": "int",
                "value": hs["ack_timeout_max_ms"],
            },
            {
                "name": "HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS",
                "type": "int",
                "value": hs["response_timeout_min_ms"],
            },
            {
                "name": "HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS",
                "type": "int",
                "value": hs["response_timeout_max_ms"],
            },
            {"name": "HANDSHAKE_RETRY_LIMIT_MIN", "type": "int", "value": hs["retry_limit_min"]},
            {"name": "HANDSHAKE_RETRY_LIMIT_MAX", "type": "int", "value": hs["retry_limit_max"]},
            {
                "name": "HANDSHAKE_HKDF_OUTPUT_LENGTH",
                "type": "int",
                "value": hs["hkdf_output_length"],
            },
            {
                "name": "HANDSHAKE_NONCE_RANDOM_BYTES",
                "type": "int",
                "value": hs["nonce_random_bytes"],
            },
            {
                "name": "HANDSHAKE_NONCE_COUNTER_BYTES",
                "type": "int",
                "value": hs["nonce_counter_bytes"],
            },
        ]

        handshake_strings = {
            "HANDSHAKE_TAG_ALGORITHM": hs["tag_algorithm"],
            "HANDSHAKE_TAG_DESCRIPTION": hs["tag_description"],
            "HANDSHAKE_HKDF_ALGORITHM": hs["hkdf_algorithm"],
            "HANDSHAKE_NONCE_FORMAT_DESCRIPTION": hs["nonce_format_description"],
        }

        handshake_bytes = {
            "HANDSHAKE_HKDF_SALT": hs["hkdf_salt"],
            "HANDSHAKE_HKDF_INFO_AUTH": hs["hkdf_info_auth"],
        }

        # Group actions
        grouped_actions = []
        action_map = {}
        for act in spec.actions:
            if "_" not in act["name"]:
                continue
            prefix, suffix = act["name"].split("_", 1)
            action_map.setdefault(prefix, []).append(  # type: ignore[reportUnknownMemberType]
                {
                    "name": suffix,
                    "value": act["value"],
                    "description": act["description"],
                }
            )

        for prefix, items in action_map.items():  # type: ignore[reportUnknownVariableType]
            cls_name = (
                "DatastoreAction"
                if prefix == "DATASTORE"
                else f"{prefix.lower().title()}Action"  # type: ignore[reportUnknownMemberType]
            )
            grouped_actions.append({"class_name": cls_name, "action_items": items})  # type: ignore[reportUnknownMemberType]

        # Process subscriptions
        subscriptions = []
        for sub in spec.mqtt_subscriptions:
            segments = []
            topic_str = sub["topic"]
            for s in sub.get("segments", []):
                if s == "+":
                    segments.append("MQTT_WILDCARD_SINGLE")  # type: ignore[reportUnknownMemberType]
                elif s == "#":
                    segments.append("MQTT_WILDCARD_MULTI")  # type: ignore[reportUnknownMemberType]
                else:
                    mapped = False
                    if topic_str in [
                        "DIGITAL",
                        "ANALOG",
                        "CONSOLE",
                        "DATASTORE",
                        "MAILBOX",
                        "SHELL",
                        "SYSTEM",
                        "FILE",
                    ]:
                        c_name = (
                            "DatastoreAction"
                            if topic_str == "DATASTORE"
                            else f"{topic_str.lower().title()}Action"
                        )
                        for act in spec.actions:
                            if act["name"].startswith(f"{topic_str}_") and act["value"] == s:
                                sfx = act["name"].split("_", 1)[1]
                                segments.append(f"{c_name}.{sfx}.value")  # type: ignore[reportUnknownMemberType]
                                mapped = True
                                break
                    if not mapped:
                        segments.append(f'"{s}"')  # type: ignore[reportUnknownMemberType]

            subscriptions.append(  # type: ignore[reportUnknownMemberType]
                {
                    "topic": topic_str,
                    "qos": sub["qos"],
                    "segments_tuple": f"({', '.join(segments)},)" if segments else "()",  # type: ignore[reportUnknownArgumentType]
                }
            )

        render = template.render(
            constants=constants,
            handshake_constants=handshake_constants,
            handshake_strings=handshake_strings,
            handshake_bytes=handshake_bytes,
            capabilities=spec.capabilities,
            architectures=spec.architectures,
            status_reasons=spec.status_reasons,
            statuses=spec.statuses,
            commands=spec.commands,
            ack_commands=[c for c in spec.commands if c.requires_ack],
            response_only_commands=[
                c for c in spec.commands if c.expects_direct_response
            ],
            topics=spec.topics,
            grouped_actions=grouped_actions,
            subscriptions=subscriptions,
        )
        out_path.write_text(render, encoding="utf-8")

    def generate_structures_packets(self, proto_path: Path, structures_path: Path) -> None:
        """Generate Packet classes from proto and splice into structures.py."""
        messages = parse_proto_messages(proto_path)
        packet_messages = [m for m in messages if m.name not in PACKET_EXCLUDE]

        packets: list[dict[str, object]] = []
        for msg in packet_messages:
            fields: list[dict[str, str]] = []
            for f in msg.fields:
                py_type = PROTO_PYTHON_TYPE_MAP.get(f.proto_type)
                if py_type is None:
                    sys.stderr.write(
                        f"Warning: unknown proto type '{f.proto_type}' "
                        f"in {msg.name}.{f.name}, skipping field\n"
                    )
                    continue
                fields.append({"name": f.name, "python_type": py_type})
            packets.append({
                "class_name": packet_class_name(msg.name),
                "proto_name": msg.name,
                "fields": fields,
            })

        template = self.env.get_template("structures_packets.py.j2")
        generated = template.render(packets=packets)
        # Normalize to exactly 2 blank lines between top-level defs (PEP 8)
        generated = re.sub(r"\n{4,}", "\n\n\n", generated)

        # Splice into structures.py between markers
        content = structures_path.read_text(encoding="utf-8")
        begin_marker = "# --- BEGIN GENERATED PACKETS ---"
        end_marker = "# --- END GENERATED PACKETS ---"

        begin_idx = content.find(begin_marker)
        end_idx = content.find(end_marker)
        if begin_idx == -1 or end_idx == -1:
            sys.stderr.write(
                f"Error: markers not found in {structures_path}. "
                f"Expected '{begin_marker}' and '{end_marker}'\n"
            )
            sys.exit(1)

        end_idx += len(end_marker)
        new_content = content[:begin_idx] + generated + content[end_idx:]
        # Normalize excessive blank lines at splice boundaries (PEP 8: max 2 between top-level defs)
        new_content = re.sub(r"\n{4,}", "\n\n\n", new_content)
        structures_path.write_text(new_content, encoding="utf-8")

    def generate_python_client(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("protocol_client.py.j2")

        c = spec.constants
        constants = [
            {"name": "PROTOCOL_VERSION", "type": "int", "value": c["protocol_version"]},
            {"name": "DEFAULT_BAUDRATE", "type": "int", "value": c["default_baudrate"]},
            {"name": "MAX_PAYLOAD_SIZE", "type": "int", "value": c["max_payload_size"]},
            {"name": "MAX_FILEPATH_LENGTH", "type": "int", "value": c["max_filepath_length"]},
            {"name": "MAX_DATASTORE_KEY_LENGTH", "type": "int", "value": c["max_datastore_key_length"]},
        ]

        render = template.render(
            constants=constants,
            capabilities=spec.capabilities,
            statuses=spec.statuses,
            commands=spec.commands,
            topics=spec.topics,
        )
        out_path.write_text(render, encoding="utf-8")


def read_version() -> str:
    if not VERSION_PATH.exists():
        sys.stderr.write(f"Warning: VERSION file not found at {VERSION_PATH}, using fallback.\n")
        return "0.0.0"
    return VERSION_PATH.read_text(encoding="utf-8").strip()


def update_metadata(version: str):
    # 1. pyproject.toml
    pyproj = REPO_ROOT / "pyproject.toml"
    if pyproj.exists():
        content = pyproj.read_text(encoding="utf-8")
        content = re.sub(r'version\s*=\s*"[^"]+"', f'version = "{version}"', content, count=1)
        pyproj.write_text(content, encoding="utf-8")
        sys.stderr.write(f"Updated {pyproj} to version {version}\n")

    # 2. mcubridge/Makefile
    makefile = REPO_ROOT / "mcubridge" / "Makefile"
    if makefile.exists():
        content = makefile.read_text(encoding="utf-8")
        content = re.sub(r'PKG_VERSION:=[^\n]+', f'PKG_VERSION:={version}', content)
        makefile.write_text(content, encoding="utf-8")
        sys.stderr.write(f"Updated {makefile} to version {version}\n")

    # 3. mcubridge-library-arduino/library.properties
    lib_prop = REPO_ROOT / "mcubridge-library-arduino" / "library.properties"
    if lib_prop.exists():
        content = lib_prop.read_text(encoding="utf-8")
        content = re.sub(r'version=[^\n]+', f'version={version}', content)
        lib_prop.write_text(content, encoding="utf-8")
        sys.stderr.write(f"Updated {lib_prop} to version {version}\n")


@app.command()
def main(
    spec_path: Annotated[Path, typer.Option("--spec", help="Protocol specification file")],
    cpp: Annotated[Optional[Path], typer.Option("--cpp", help="C++ header output")] = None,
    cpp_structs: Annotated[
        Optional[Path], typer.Option("--cpp-structs", help="C++ structs output")
    ] = None,
    py: Annotated[Optional[Path], typer.Option("--py", help="Python output")] = None,
    py_client: Annotated[Optional[Path], typer.Option("--py-client", help="Python client output")] = None,
    structures: Annotated[
        Optional[Path], typer.Option("--structures", help="Splice generated Packets into structures.py")
    ] = None,
) -> None:
    spec = ProtocolSpec.load(spec_path)
    gen = JinjaGenerator()
    version = read_version()

    update_metadata(version)

    if cpp:
        cpp.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_header(spec, cpp, version)
        sys.stderr.write(f"Generated {cpp}\n")

        # Generate hardware config next to the main header
        hw_config_path = cpp.parent / "rpc_hw_config.h"
        gen.generate_cpp_hw_config(spec, hw_config_path)
        sys.stderr.write(f"Generated {hw_config_path}\n")

    if cpp_structs:
        cpp_structs.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_structs(spec, cpp_structs)
        sys.stderr.write(f"Generated {cpp_structs}\n")

    if py:
        py.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_python(spec, py)
        sys.stderr.write(f"Generated {py}\n")

    # [PHASE 3] Protobuf & Nanopb compilation
    proto_dir = spec_path.parent
    py_out = py.parent if py else REPO_ROOT / "mcubridge/mcubridge/protocol"
    cpp_out = cpp.parent if cpp else REPO_ROOT / "mcubridge-library-arduino/src/protocol"

    py_out.mkdir(parents=True, exist_ok=True)
    cpp_out.mkdir(parents=True, exist_ok=True)

    nanopb_plugin = shutil.which("protoc-gen-nanopb")
    if nanopb_plugin is None:
        sys.stderr.write(
            "Error: protoc-gen-nanopb not found. Install nanopb: pip install nanopb\n"
        )
        sys.exit(1)

    # Step 1: Generate Python protobuf bindings + type stubs
    for proto_file in ("nanopb.proto", "mcubridge.proto"):
        py_cmd = [
            sys.executable, "-m", "grpc_tools.protoc",
            f"--proto_path={proto_dir}",
            f"--python_out={py_out}",
            f"--pyi_out={py_out}",
            proto_file,
        ]
        try:
            subprocess.check_call(py_cmd)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            sys.stderr.write(f"Error generating Python protobuf for {proto_file}: {e}\n")
            sys.exit(1)

    # Fix relative imports
    for suffix in (".py", ".pyi"):
        pb2_file = py_out / f"mcubridge_pb2{suffix}"
        if pb2_file.exists():
            content = pb2_file.read_text(encoding="utf-8")
            content = content.replace("import nanopb_pb2", "from . import nanopb_pb2")
            pb2_file.write_text(content, encoding="utf-8")

    # Strip per-class DESCRIPTOR declarations
    pyi_file = py_out / "mcubridge_pb2.pyi"
    if pyi_file.exists():
        pyi_content = pyi_file.read_text(encoding="utf-8")
        pyi_content = re.sub(
            r"^    DESCRIPTOR: _ClassVar\[_descriptor\.Descriptor\]\n",
            "",
            pyi_content,
            flags=re.MULTILINE,
        )
        pyi_content = re.sub(
            r"^from \. import nanopb_pb2 as _nanopb_pb2\n",
            "",
            pyi_content,
            flags=re.MULTILINE,
        )
        pyi_file.write_text(pyi_content, encoding="utf-8")

    nanopb_pyi = py_out / "nanopb_pb2.pyi"
    if nanopb_pyi.exists():
        nanopb_pyi.unlink()

    sys.stderr.write(f"Generated Python protobuf bindings in {py_out}\n")

    # Step 2: Generate C nanopb bindings
    options_file = proto_dir / "mcubridge.options"
    nanopb_out_arg = f"-f{options_file}:{cpp_out}" if options_file.exists() else str(cpp_out)
    c_cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"--proto_path={proto_dir}",
        f"--plugin=protoc-gen-nanopb={nanopb_plugin}",
        f"--nanopb_out={nanopb_out_arg}",
        "mcubridge.proto",
    ]
    try:
        subprocess.check_call(c_cmd)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.stderr.write(f"Error generating nanopb C bindings: {e}\n")
        sys.exit(1)

    # Post-process: rewrite nanopb includes
    pb_h = cpp_out / "mcubridge.pb.h"
    if pb_h.exists():
        pb_h.write_text(
            pb_h.read_text(encoding="utf-8").replace('#include <pb.h>', '#include "nanopb/pb.h"'),
            encoding="utf-8",
        )
    sys.stderr.write(f"Generated nanopb C bindings in {cpp_out}\n")

    if py_client:
        py_client.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_python_client(spec, py_client)
        sys.stderr.write(f"Generated {py_client}\n")

    # Step 3: Generate Packet classes from proto into structures.py
    if structures:
        proto_path = spec_path.parent / "mcubridge.proto"
        gen.generate_structures_packets(proto_path, structures)
        sys.stderr.write(f"Generated Packet classes in {structures}\n")

    # Step 4: Generate type stubs for untyped libraries using pyright
    untyped_libs = ["transitions", "diskcache"]
    sys.stderr.write(f"Generating type stubs for {', '.join(untyped_libs)}...\n")
    for lib in untyped_libs:
        stub_cmd = [sys.executable, "-m", "pyright", "--createstub", lib]
        try:
            # We use subprocess.run to allow failure if pyright is not available,
            # but log a warning.
            subprocess.run(stub_cmd, check=False, capture_output=True)
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to generate stubs for {lib}: {e}\n")


if __name__ == "__main__":
    app()
