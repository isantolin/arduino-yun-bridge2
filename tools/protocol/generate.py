#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Jinja2: Declarative templates for C++ and Python outputs.

Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Annotated

import msgspec
import typer
from jinja2 import Environment, FileSystemLoader

app = typer.Typer(help="Protocol binding generator for MCU Bridge v2.")

TEMPLATE_DIR = Path(__file__).parent / "templates"


# =============================================================================
# 1. Model: Protocol Specification (The "Data")
# =============================================================================


class CommandDef(msgspec.Struct, frozen=True):
    name: str
    value: int
    directions: list[str]
    category: Optional[str] = None
    description: Optional[str] = None
    requires_ack: bool = False
    expects_direct_response: bool = False


class StatusDef(msgspec.Struct, frozen=True):
    name: str
    value: int
    description: str


class StructField(msgspec.Struct, frozen=True):
    name: str
    type_code: str  # B, H, I, Q

    @property
    def cpp_type(self) -> str:
        return {"B": "uint8_t", "H": "uint16_t", "I": "uint32_t", "Q": "uint64_t"}[self.type_code]

    @property
    def size(self) -> int:
        return {"B": 1, "H": 2, "I": 4, "Q": 8}[self.type_code]

    @property
    def read_func(self) -> Optional[str]:
        return {
            "B": None,
            "H": "rpc::read_u16_be",
            "I": "rpc::read_u32_be",
            "Q": "rpc::read_u64_be",
        }[self.type_code]

    @property
    def write_func(self) -> Optional[str]:
        func = self.read_func
        return func.replace("read_", "write_") if func else None


class PayloadDef(msgspec.Struct, frozen=True):
    name: str
    fields: list[StructField]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.fields)

    @property
    def all_bytes(self) -> bool:
        return all(not f.read_func for f in self.fields)

    @property
    def byte_inits(self) -> str:
        return ", ".join(f"data[{i}]" for i in range(len(self.fields)))


class RawProtocolData(msgspec.Struct):
    constants: dict[str, Any]
    commands: list[dict[str, Any]]
    statuses: list[dict[str, Any]]
    payloads: dict[str, dict[str, str]]
    handshake: dict[str, Any]
    mqtt_subscriptions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    topics: list[dict[str, Any]]
    capabilities: dict[str, int]
    architectures: dict[str, int]
    status_reasons: dict[str, str]


@dataclass
class ProtocolSpec:
    """Root model of the parsed spec.toml."""

    constants: dict[str, Any]
    commands: list[CommandDef]
    statuses: list[StatusDef]
    payloads: dict[str, PayloadDef]
    handshake: dict[str, Any]
    mqtt_subscriptions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    topics: list[dict[str, Any]]
    capabilities: dict[str, int]
    architectures: dict[str, int]
    status_reasons: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> ProtocolSpec:
        with path.open("rb") as f:
            raw = msgspec.toml.decode(f.read(), type=RawProtocolData)

        # Convert raw dicts to Structs
        cmds = [msgspec.convert(c, CommandDef) for c in raw.commands]
        statuses = [msgspec.convert(s, StatusDef) for s in raw.statuses]

        payloads = {}
        for name, fields_dict in raw.payloads.items():
            fields = [StructField(k, v) for k, v in fields_dict.items()]
            payloads[name] = PayloadDef(name, fields)

        return cls(
            constants=raw.constants,
            commands=cmds,
            statuses=statuses,
            payloads=payloads,
            handshake=raw.handshake,
            mqtt_subscriptions=raw.mqtt_subscriptions,
            actions=raw.actions,
            topics=raw.topics,
            capabilities=raw.capabilities,
            architectures=raw.architectures,
            status_reasons=raw.status_reasons,
        )


# =============================================================================
# 2. Generators: The Logic
# =============================================================================


class JinjaGenerator:
    def __init__(self) -> None:
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            keep_trailing_newline=True,
        )

    def generate_cpp_header(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("rpc_protocol.h.j2")

        constants = [
            {"name": "PROTOCOL_VERSION", "type": "uint8_t", "value": spec.constants["protocol_version"]},
            {"name": "RPC_DEFAULT_BAUDRATE", "type": "unsigned long", "value": spec.constants["default_baudrate"]},
            {"name": "MAX_PAYLOAD_SIZE", "type": "size_t", "value": spec.constants["max_payload_size"]},
            {"name": "RPC_DEFAULT_SAFE_BAUDRATE", "type": "unsigned long", "value": spec.constants["default_safe_baudrate"]},
            {"name": "RPC_MAX_FILEPATH_LENGTH", "type": "size_t", "value": spec.constants["max_filepath_length"]},
            {"name": "RPC_MAX_DATASTORE_KEY_LENGTH", "type": "size_t", "value": spec.constants["max_datastore_key_length"]},
            {"name": "RPC_DEFAULT_ACK_TIMEOUT_MS", "type": "unsigned int", "value": spec.constants["default_ack_timeout_ms"]},
            {"name": "RPC_DEFAULT_RETRY_LIMIT", "type": "uint8_t", "value": spec.constants["default_retry_limit"]},
            {"name": "RPC_MAX_PENDING_TX_FRAMES", "type": "uint8_t", "value": spec.constants["max_pending_tx_frames"]},
            {"name": "RPC_INVALID_ID_SENTINEL", "type": "uint16_t", "value": spec.constants["invalid_id_sentinel"]},
            {"name": "RPC_CMD_FLAG_COMPRESSED", "type": "uint16_t", "value": spec.constants["cmd_flag_compressed"]},
            {"name": "RPC_UINT8_MASK", "type": "uint8_t", "value": spec.constants["uint8_mask"]},
            {"name": "RPC_UINT16_MAX", "type": "uint16_t", "value": spec.constants["uint16_max"]},
            {"name": "RPC_PROCESS_DEFAULT_EXIT_CODE", "type": "uint8_t", "value": spec.constants["process_default_exit_code"]},
            {"name": "RPC_CRC32_MASK", "type": "uint32_t", "value": spec.constants["crc32_mask"]},
            {"name": "RPC_CRC_INITIAL", "type": "uint32_t", "value": spec.constants["crc_initial"]},
            {"name": "RPC_CRC_POLYNOMIAL", "type": "uint32_t", "value": spec.constants["crc_polynomial"]},
            {"name": "RPC_FRAME_DELIMITER", "type": "uint8_t", "value": spec.constants["frame_delimiter"]},
            {"name": "RPC_DIGITAL_LOW", "type": "uint8_t", "value": spec.constants["digital_low"]},
            {"name": "RPC_DIGITAL_HIGH", "type": "uint8_t", "value": spec.constants["digital_high"]},
            {"name": "RPC_RLE_ESCAPE_BYTE", "type": "uint8_t", "value": spec.constants["rle_escape_byte"]},
            {"name": "RPC_RLE_MIN_RUN_LENGTH", "type": "uint8_t", "value": spec.constants["rle_min_run_length"]},
            {"name": "RPC_RLE_MAX_RUN_LENGTH", "type": "uint16_t", "value": spec.constants["rle_max_run_length"]},
            {"name": "RPC_RLE_SINGLE_ESCAPE_MARKER", "type": "uint8_t", "value": spec.constants["rle_single_escape_marker"]},
            {"name": "RPC_STATUS_CODE_MIN", "type": "uint8_t", "value": spec.constants["status_code_min"]},
            {"name": "RPC_STATUS_CODE_MAX", "type": "uint8_t", "value": spec.constants["status_code_max"]},
            {"name": "RPC_SYSTEM_COMMAND_MIN", "type": "uint16_t", "value": spec.constants["system_command_min"]},
            {"name": "RPC_SYSTEM_COMMAND_MAX", "type": "uint16_t", "value": spec.constants["system_command_max"]},
            {"name": "RPC_GPIO_COMMAND_MIN", "type": "uint16_t", "value": spec.constants["gpio_command_min"]},
            {"name": "RPC_GPIO_COMMAND_MAX", "type": "uint16_t", "value": spec.constants["gpio_command_max"]},
            {"name": "RPC_CONSOLE_COMMAND_MIN", "type": "uint16_t", "value": spec.constants["console_command_min"]},
            {"name": "RPC_CONSOLE_COMMAND_MAX", "type": "uint16_t", "value": spec.constants["console_command_max"]},
            {"name": "RPC_DATASTORE_COMMAND_MIN", "type": "uint16_t", "value": spec.constants["datastore_command_min"]},
            {"name": "RPC_DATASTORE_COMMAND_MAX", "type": "uint16_t", "value": spec.constants["datastore_command_max"]},
            {"name": "RPC_MAILBOX_COMMAND_MIN", "type": "uint16_t", "value": spec.constants["mailbox_command_min"]},
            {"name": "RPC_MAILBOX_COMMAND_MAX", "type": "uint16_t", "value": spec.constants["mailbox_command_max"]},
            {"name": "RPC_FILESYSTEM_COMMAND_MIN", "type": "uint16_t", "value": spec.constants["filesystem_command_min"]},
            {"name": "RPC_FILESYSTEM_COMMAND_MAX", "type": "uint16_t", "value": spec.constants["filesystem_command_max"]},
            {"name": "RPC_PROCESS_COMMAND_MIN", "type": "uint16_t", "value": spec.constants["process_command_min"]},
            {"name": "RPC_PROCESS_COMMAND_MAX", "type": "uint16_t", "value": spec.constants["process_command_max"]},
        ]

        hs = spec.handshake
        handshake_constants = [
            {"name": "RPC_HANDSHAKE_NONCE_LENGTH", "type": "unsigned int", "value": hs["nonce_length"]},
            {"name": "RPC_HANDSHAKE_TAG_LENGTH", "type": "unsigned int", "value": hs["tag_length"]},
            {"name": "RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS", "type": "uint32_t", "value": hs["ack_timeout_min_ms"]},
            {"name": "RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS", "type": "uint32_t", "value": hs["ack_timeout_max_ms"]},
            {"name": "RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS", "type": "uint32_t", "value": hs["response_timeout_min_ms"]},
            {"name": "RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS", "type": "uint32_t", "value": hs["response_timeout_max_ms"]},
            {"name": "RPC_HANDSHAKE_RETRY_LIMIT_MIN", "type": "unsigned int", "value": hs["retry_limit_min"]},
            {"name": "RPC_HANDSHAKE_RETRY_LIMIT_MAX", "type": "unsigned int", "value": hs["retry_limit_max"]},
            {"name": "RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH", "type": "unsigned int", "value": hs["hkdf_output_length"]},
            {"name": "RPC_HANDSHAKE_NONCE_RANDOM_BYTES", "type": "unsigned int", "value": hs["nonce_random_bytes"]},
            {"name": "RPC_HANDSHAKE_NONCE_COUNTER_BYTES", "type": "unsigned int", "value": hs["nonce_counter_bytes"]},
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

        complex_payloads = [
            "struct ConsoleWrite { const uint8_t* data; size_t length; static ConsoleWrite parse(const uint8_t* d, size_t l) { return {d, l}; } };",
            "struct DatastoreGet { etl::string_view key; static DatastoreGet parse(const uint8_t* d) { return {etl::string_view(reinterpret_cast<const char*>(d + 1), d[0])}; } };",
            "struct DatastoreGetResponse { const uint8_t* value; uint8_t value_len; static DatastoreGetResponse parse(const uint8_t* d) { return {d + 1, d[0]}; } };",
            "struct DatastorePut { etl::string_view key; const uint8_t* value; uint8_t value_len; static DatastorePut parse(const uint8_t* d) { uint8_t k = d[0]; return {etl::string_view(reinterpret_cast<const char*>(d + 1), k), d + 1 + k + 1, d[1 + k]}; } };",
            "struct MailboxPush { const uint8_t* data; uint16_t length; static MailboxPush parse(const uint8_t* d) { return {d + 2, rpc::read_u16_be(d)}; } };",
            "struct MailboxReadResponse { const uint8_t* content; uint16_t length; static MailboxReadResponse parse(const uint8_t* d) { return {d + 2, rpc::read_u16_be(d)}; } };",
            "struct FileWrite { etl::string_view path; const uint8_t* data; uint16_t data_len; static FileWrite parse(const uint8_t* d) { uint8_t p = d[0]; return {etl::string_view(reinterpret_cast<const char*>(d + 1), p), d + 1 + p + 2, rpc::read_u16_be(d + 1 + p)}; } };",
            "struct FileRead { etl::string_view path; static FileRead parse(const uint8_t* d) { return {etl::string_view(reinterpret_cast<const char*>(d + 1), d[0])}; } };",
            "struct FileReadResponse { const uint8_t* content; uint16_t length; static FileReadResponse parse(const uint8_t* d) { return {d + 2, rpc::read_u16_be(d)}; } };",
            "struct FileRemove { etl::string_view path; static FileRemove parse(const uint8_t* d) { return {etl::string_view(reinterpret_cast<const char*>(d + 1), d[0])}; } };",
            "struct ProcessRun { etl::string_view command; static ProcessRun parse(const uint8_t* d, size_t l) { return {etl::string_view(reinterpret_cast<const char*>(d), l)}; } };",
            "struct ProcessRunAsync { etl::string_view command; static ProcessRunAsync parse(const uint8_t* d, size_t l) { return {etl::string_view(reinterpret_cast<const char*>(d), l)}; } };",
            "struct ProcessRunResponse { uint8_t status; const uint8_t* stdout_data; uint16_t stdout_len; const uint8_t* stderr_data; uint16_t stderr_len; uint8_t exit_code; static ProcessRunResponse parse(const uint8_t* d) { ProcessRunResponse m; m.status = d[0]; m.stdout_len = rpc::read_u16_be(d + 1); m.stdout_data = d + 3; m.stderr_len = rpc::read_u16_be(d + 3 + m.stdout_len); m.stderr_data = d + 3 + m.stdout_len + 2; m.exit_code = d[3 + m.stdout_len + 2 + m.stderr_len]; return m; } };",
            "struct ProcessPollResponse { uint8_t status; uint8_t exit_code; const uint8_t* stdout_data; uint16_t stdout_len; const uint8_t* stderr_data; uint16_t stderr_len; static ProcessPollResponse parse(const uint8_t* d) { ProcessPollResponse m; m.status = d[0]; m.exit_code = d[1]; m.stdout_len = rpc::read_u16_be(d + 2); m.stdout_data = d + 4; m.stderr_len = rpc::read_u16_be(d + 4 + m.stdout_len); m.stderr_data = d + 4 + m.stdout_len + 2; return m; } };",
        ]

        manual_specs = [
            {"name": "payload::ConsoleWrite", "body": ["return etl::expected<payload::ConsoleWrite, rpc::FrameError>(payload::ConsoleWrite::parse(frame.payload.data(), frame.header.payload_length));"]},
            {"name": "payload::ProcessRun", "body": ["return etl::expected<payload::ProcessRun, rpc::FrameError>(payload::ProcessRun::parse(frame.payload.data(), frame.header.payload_length));"]},
            {"name": "payload::ProcessRunAsync", "body": ["return etl::expected<payload::ProcessRunAsync, rpc::FrameError>(payload::ProcessRunAsync::parse(frame.payload.data(), frame.header.payload_length));"]},
            {"name": "payload::DatastoreGet", "body": ["if (frame.header.payload_length < 1 || frame.header.payload_length < (size_t)(frame.payload[0] + 1)) { return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED); }", "return etl::expected<payload::DatastoreGet, rpc::FrameError>(payload::DatastoreGet::parse(frame.payload.data()));"]},
            {"name": "payload::DatastoreGetResponse", "body": ["if (frame.header.payload_length < 1 || frame.header.payload_length < (size_t)(frame.payload[0] + 1)) { return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED); }", "return etl::expected<payload::DatastoreGetResponse, rpc::FrameError>(payload::DatastoreGetResponse::parse(frame.payload.data()));"]},
            {"name": "payload::DatastorePut", "body": ["if (frame.header.payload_length < 2) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint8_t k = frame.payload[0];", "if (frame.header.payload_length < (size_t)(k + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint8_t v = frame.payload[k + 1];", "if (frame.header.payload_length < (size_t)(k + v + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "return etl::expected<payload::DatastorePut, rpc::FrameError>(payload::DatastorePut::parse(frame.payload.data()));"]},
            {"name": "payload::MailboxPush", "body": ["if (frame.header.payload_length < 2) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint16_t l = rpc::read_u16_be(frame.payload.data());", "if (frame.header.payload_length < (size_t)(l + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "return etl::expected<payload::MailboxPush, rpc::FrameError>(payload::MailboxPush::parse(frame.payload.data()));"]},
            {"name": "payload::MailboxReadResponse", "body": ["if (frame.header.payload_length < 2) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint16_t l = rpc::read_u16_be(frame.payload.data());", "if (frame.header.payload_length < (size_t)(l + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "return etl::expected<payload::MailboxReadResponse, rpc::FrameError>(payload::MailboxReadResponse::parse(frame.payload.data()));"]},
            {"name": "payload::FileWrite", "body": ["if (frame.header.payload_length < 3) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint8_t p = frame.payload[0];", "if (frame.header.payload_length < (size_t)(p + 3)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint16_t d = rpc::read_u16_be(frame.payload.data() + 1 + p);", "if (frame.header.payload_length < (size_t)(p + d + 3)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "return etl::expected<payload::FileWrite, rpc::FrameError>(payload::FileWrite::parse(frame.payload.data()));"]},
            {"name": "payload::FileRead", "body": ["if (frame.header.payload_length < 1 || frame.header.payload_length < (size_t)(frame.payload[0] + 1)) { return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED); }", "return etl::expected<payload::FileRead, rpc::FrameError>(payload::FileRead::parse(frame.payload.data()));"]},
            {"name": "payload::FileReadResponse", "body": ["if (frame.header.payload_length < 2) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint16_t l = rpc::read_u16_be(frame.payload.data());", "if (frame.header.payload_length < (size_t)(l + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "return etl::expected<payload::FileReadResponse, rpc::FrameError>(payload::FileReadResponse::parse(frame.payload.data()));"]},
            {"name": "payload::FileRemove", "body": ["if (frame.header.payload_length < 1 || frame.header.payload_length < (size_t)(frame.payload[0] + 1)) { return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED); }", "return etl::expected<payload::FileRemove, rpc::FrameError>(payload::FileRemove::parse(frame.payload.data()));"]},
            {"name": "payload::ProcessRunResponse", "body": ["if (frame.header.payload_length < 6) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint16_t o = rpc::read_u16_be(frame.payload.data() + 1);", "if (frame.header.payload_length < (size_t)(o + 5)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint16_t e = rpc::read_u16_be(frame.payload.data() + 3 + o);", "if (frame.header.payload_length < (size_t)(o + e + 6)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "return etl::expected<payload::ProcessRunResponse, rpc::FrameError>(payload::ProcessRunResponse::parse(frame.payload.data()));"]},
            {"name": "payload::ProcessPollResponse", "body": ["if (frame.header.payload_length < 6) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint16_t o = rpc::read_u16_be(frame.payload.data() + 2);", "if (frame.header.payload_length < (size_t)(o + 6)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "uint16_t e = rpc::read_u16_be(frame.payload.data() + 4 + o);", "if (frame.header.payload_length < (size_t)(o + e + 6)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);", "return etl::expected<payload::ProcessPollResponse, rpc::FrameError>(payload::ProcessPollResponse::parse(frame.payload.data()));"]},
        ]

        render = template.render(
            payloads=spec.payloads.values(),
            complex_payloads=complex_payloads,
            manual_parse_specializations=manual_specs,
        )
        out_path.write_text(render, encoding="utf-8")

    def generate_python(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("protocol.py.j2")

        constants = [
            {"name": "PROTOCOL_VERSION", "type": "int", "value": spec.constants["protocol_version"]},
            {"name": "DEFAULT_BAUDRATE", "type": "int", "value": spec.constants["default_baudrate"]},
            {"name": "MAX_PAYLOAD_SIZE", "type": "int", "value": spec.constants["max_payload_size"]},
            {"name": "DEFAULT_SAFE_BAUDRATE", "type": "int", "value": spec.constants["default_safe_baudrate"]},
            {"name": "MAX_FILEPATH_LENGTH", "type": "int", "value": spec.constants["max_filepath_length"]},
            {"name": "MAX_DATASTORE_KEY_LENGTH", "type": "int", "value": spec.constants["max_datastore_key_length"]},
            {"name": "DEFAULT_ACK_TIMEOUT_MS", "type": "int", "value": spec.constants["default_ack_timeout_ms"]},
            {"name": "DEFAULT_RETRY_LIMIT", "type": "int", "value": spec.constants["default_retry_limit"]},
            {"name": "MAX_PENDING_TX_FRAMES", "type": "int", "value": spec.constants["max_pending_tx_frames"]},
            {"name": "INVALID_ID_SENTINEL", "type": "int", "value": spec.constants["invalid_id_sentinel"]},
            {"name": "CMD_FLAG_COMPRESSED", "type": "int", "value": spec.constants["cmd_flag_compressed"]},
            {"name": "UINT8_MASK", "type": "int", "value": spec.constants["uint8_mask"]},
            {"name": "UINT16_MAX", "type": "int", "value": spec.constants["uint16_max"]},
            {"name": "PROCESS_DEFAULT_EXIT_CODE", "type": "int", "value": spec.constants["process_default_exit_code"]},
            {"name": "CRC32_MASK", "type": "int", "value": spec.constants["crc32_mask"]},
            {"name": "CRC_INITIAL", "type": "int", "value": spec.constants["crc_initial"]},
            {"name": "CRC_POLYNOMIAL", "type": "int", "value": spec.constants["crc_polynomial"]},
            {"name": "FRAME_DELIMITER", "type": "bytes", "value": f"bytes([{spec.constants['frame_delimiter']}])"},
            {"name": "DIGITAL_LOW", "type": "int", "value": spec.constants["digital_low"]},
            {"name": "DIGITAL_HIGH", "type": "int", "value": spec.constants["digital_high"]},
            {"name": "RLE_ESCAPE_BYTE", "type": "int", "value": spec.constants["rle_escape_byte"]},
            {"name": "RLE_MIN_RUN_LENGTH", "type": "int", "value": spec.constants["rle_min_run_length"]},
            {"name": "RLE_MAX_RUN_LENGTH", "type": "int", "value": spec.constants["rle_max_run_length"]},
            {"name": "RLE_SINGLE_ESCAPE_MARKER", "type": "int", "value": spec.constants["rle_single_escape_marker"]},
            {"name": "STATUS_CODE_MIN", "type": "int", "value": spec.constants["status_code_min"]},
            {"name": "STATUS_CODE_MAX", "type": "int", "value": spec.constants["status_code_max"]},
            {"name": "SYSTEM_COMMAND_MIN", "type": "int", "value": spec.constants["system_command_min"]},
            {"name": "SYSTEM_COMMAND_MAX", "type": "int", "value": spec.constants["system_command_max"]},
            {"name": "GPIO_COMMAND_MIN", "type": "int", "value": spec.constants["gpio_command_min"]},
            {"name": "GPIO_COMMAND_MAX", "type": "int", "value": spec.constants["gpio_command_max"]},
            {"name": "CONSOLE_COMMAND_MIN", "type": "int", "value": spec.constants["console_command_min"]},
            {"name": "CONSOLE_COMMAND_MAX", "type": "int", "value": spec.constants["console_command_max"]},
            {"name": "DATASTORE_COMMAND_MIN", "type": "int", "value": spec.constants["datastore_command_min"]},
            {"name": "DATASTORE_COMMAND_MAX", "type": "int", "value": spec.constants["datastore_command_max"]},
            {"name": "MAILBOX_COMMAND_MIN", "type": "int", "value": spec.constants["mailbox_command_min"]},
            {"name": "MAILBOX_COMMAND_MAX", "type": "int", "value": spec.constants["mailbox_command_max"]},
            {"name": "FILESYSTEM_COMMAND_MIN", "type": "int", "value": spec.constants["filesystem_command_min"]},
            {"name": "FILESYSTEM_COMMAND_MAX", "type": "int", "value": spec.constants["filesystem_command_max"]},
            {"name": "PROCESS_COMMAND_MIN", "type": "int", "value": spec.constants["process_command_min"]},
            {"name": "PROCESS_COMMAND_MAX", "type": "int", "value": spec.constants["process_command_max"]},
        ]

        hs = spec.handshake
        handshake_constants = [
            {"name": "HANDSHAKE_NONCE_LENGTH", "type": "int", "value": hs["nonce_length"]},
            {"name": "HANDSHAKE_TAG_LENGTH", "type": "int", "value": hs["tag_length"]},
            {"name": "HANDSHAKE_ACK_TIMEOUT_MIN_MS", "type": "int", "value": hs["ack_timeout_min_ms"]},
            {"name": "HANDSHAKE_ACK_TIMEOUT_MAX_MS", "type": "int", "value": hs["ack_timeout_max_ms"]},
            {"name": "HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS", "type": "int", "value": hs["response_timeout_min_ms"]},
            {"name": "HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS", "type": "int", "value": hs["response_timeout_max_ms"]},
            {"name": "HANDSHAKE_RETRY_LIMIT_MIN", "type": "int", "value": hs["retry_limit_min"]},
            {"name": "HANDSHAKE_RETRY_LIMIT_MAX", "type": "int", "value": hs["retry_limit_max"]},
            {"name": "HANDSHAKE_HKDF_OUTPUT_LENGTH", "type": "int", "value": hs["hkdf_output_length"]},
            {"name": "HANDSHAKE_NONCE_RANDOM_BYTES", "type": "int", "value": hs["nonce_random_bytes"]},
            {"name": "HANDSHAKE_NONCE_COUNTER_BYTES", "type": "int", "value": hs["nonce_counter_bytes"]},
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
            if "_" not in act["name"]: continue
            prefix, suffix = act["name"].split("_", 1)
            action_map.setdefault(prefix, []).append({
                "name": suffix,
                "value": act["value"],
                "description": act["description"]
            })
        
        for prefix, items in action_map.items():
            cls_name = "DatastoreAction" if prefix == "DATASTORE" else f"{prefix.lower().title()}Action"
            grouped_actions.append({"class_name": cls_name, "action_items": items})

        # Process subscriptions
        subscriptions = []
        for sub in spec.mqtt_subscriptions:
            segments = []
            topic = sub["topic"]
            for s in sub.get("segments", []):
                if s == "+": segments.append("MQTT_WILDCARD_SINGLE")
                elif s == "#": segments.append("MQTT_WILDCARD_MULTI")
                else:
                    mapped = False
                    if topic in ["DIGITAL", "ANALOG", "CONSOLE", "DATASTORE", "MAILBOX", "SHELL", "SYSTEM", "FILE"]:
                        cls_name = "DatastoreAction" if topic == "DATASTORE" else f"{topic.lower().title()}Action"
                        for act in spec.actions:
                            if act["name"].startswith(f"{topic}_") and act["value"] == s:
                                suffix = act["name"].split("_", 1)[1]
                                segments.append(f"{cls_name}.{suffix}.value")
                                mapped = True
                                break
                    if not mapped: segments.append(f'"{s}"')
            
            subscriptions.append({
                "topic": topic,
                "qos": sub["qos"],
                "segments_tuple": f"({', '.join(segments)},)" if segments else "()"
            })

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
            response_only_commands=[c for c in spec.commands if c.expects_direct_response],
            topics=spec.topics,
            grouped_actions=grouped_actions,
            subscriptions=subscriptions,
        )
        out_path.write_text(render, encoding="utf-8")


@app.command()
def main(
    spec_path: Annotated[Path, typer.Option("--spec", help="Protocol specification file")],
    cpp: Annotated[Optional[Path], typer.Option("--cpp", help="C++ header output")] = None,
    cpp_structs: Annotated[Optional[Path], typer.Option("--cpp-structs", help="C++ structs output")] = None,
    py: Annotated[Optional[Path], typer.Option("--py", help="Python output")] = None,
) -> None:
    spec = ProtocolSpec.load(spec_path)
    gen = JinjaGenerator()

    if cpp:
        cpp.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_header(spec, cpp)
        print(f"Generated {cpp}")

    if cpp_structs:
        cpp_structs.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_structs(spec, cpp_structs)
        print(f"Generated {cpp_structs}")

    if py:
        py.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_python(spec, py)
        print(f"Generated {py}")


if __name__ == "__main__":
    app()
