#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Writer: Context-aware indentation manager for code generation.
- Generators: Specialized classes for C++ and Python outputs.

Copyright (C) 2025 Ignacio Santolin and contributors
"""

from __future__ import annotations

import json
import sys
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, TextIO, Optional, Annotated

import msgspec
import typer

app = typer.Typer(help="Protocol binding generator for MCU Bridge v2.")


# =============================================================================
# 1. Utility: Code Writer (The "View" Helper)
# =============================================================================


class CodeWriter:
    """Helper to generate indented code structuredly."""

    def __init__(self, out: TextIO, indent_str: str = "    ") -> None:
        self._out = out
        self._indent_str = indent_str
        self._level = 0

    def write(self, text: str = "") -> None:
        """Write a line with current indentation."""
        if not text:
            self._out.write("\n")
            return
        self._out.write(f"{self._indent_str * self._level}{text}\n")

    def raw(self, text: str) -> None:
        """Write raw text without indentation (e.g. multiline strings)."""
        self._out.write(text)

    @contextmanager
    def indent(self) -> Iterator[None]:
        """Increase indentation level for the context block."""
        self._level += 1
        try:
            yield
        finally:
            self._level -= 1

    @contextmanager
    def block(self, start: str, end: str | None = "}") -> Iterator[None]:
        """Write a C++ style block { ... } or python block."""
        self.write(start)
        with self.indent():
            yield
        if end is not None:
            self.write(end)


# =============================================================================
# 2. Model: Protocol Specification (The "Data")
# =============================================================================


@dataclass(frozen=True)
class CommandDef:
    name: str
    value: int
    directions: list[str]
    category: str | None = None
    description: str | None = None
    requires_ack: bool = False
    expects_direct_response: bool = False


@dataclass(frozen=True)
class StatusDef:
    name: str
    value: int
    description: str


@dataclass(frozen=True)
class StructField:
    name: str
    type_code: str  # B, H, I, Q

    @property
    def cpp_type(self) -> str:
        return {"B": "uint8_t", "H": "uint16_t", "I": "uint32_t", "Q": "uint64_t"}[self.type_code]

    @property
    def size(self) -> int:
        return {"B": 1, "H": 2, "I": 4, "Q": 8}[self.type_code]

    @property
    def read_func(self) -> str | None:
        return {
            "B": None,
            "H": "rpc::read_u16_be",
            "I": "rpc::read_u32_be",
            "Q": "rpc::read_u64_be",
        }[self.type_code]

    @property
    def write_func(self) -> str | None:
        func = self.read_func
        return func.replace("read_", "write_") if func else None


@dataclass(frozen=True)
class PayloadDef:
    name: str
    fields: list[StructField]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.fields)


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
            data = msgspec.toml.decode(f.read())

        # Parse Commands
        cmds = [CommandDef(**c) for c in data.get("commands", [])]

        # Parse Statuses
        statuses = [StatusDef(**s) for s in data.get("statuses", [])]

        # Parse Payloads
        payloads = {}
        for name, fields_dict in data.get("payloads", {}).items():
            fields = [StructField(k, v) for k, v in fields_dict.items()]
            payloads[name] = PayloadDef(name, fields)

        return cls(
            constants=data.get("constants", {}),
            commands=cmds,
            statuses=statuses,
            payloads=payloads,
            handshake=data.get("handshake", {}),
            mqtt_subscriptions=data.get("mqtt_subscriptions", []),
            actions=data.get("actions", []),
            topics=data.get("topics", []),
            capabilities=data.get("capabilities", {}),
            architectures=data.get("architectures", {}),
            status_reasons=data.get("status_reasons", {}),
        )


# =============================================================================
# 3. Generators: The Logic
# =============================================================================


class CppGenerator:
    def generate_header(self, spec: ProtocolSpec, out: TextIO) -> None:
        w = CodeWriter(out)
        self._write_file_header(w)

        w.write("#ifndef RPC_PROTOCOL_H")
        w.write("#define RPC_PROTOCOL_H")
        w.write()
        w.write("#include <stddef.h>")
        w.write("#include <stdint.h>")
        w.write()

        with w.block("namespace rpc {", "} // namespace rpc"):
            self._write_constants(w, spec)
            self._write_enums(w, spec)
            self._write_helpers(w, spec)

        w.write("#endif")

    def generate_structs(self, spec: ProtocolSpec, out: TextIO) -> None:
        w = CodeWriter(out)
        self._write_file_header(w)

        w.write("#ifndef RPC_STRUCTS_H")
        w.write("#define RPC_STRUCTS_H")
        w.write()
        w.write("#include <stdint.h>")
        w.write("#include <stddef.h>")
        w.write("#include <etl/string_view.h>")
        w.write("#include <etl/optional.h>")
        w.write("#include <etl/span.h>")
        w.write('#include "rpc_protocol.h"')
        w.write('#include "rpc_frame.h"')
        w.write()

        w.write("namespace rpc {")
        with w.block("namespace payload {", "} // namespace payload"):
            self._write_auto_payloads(w, spec)
            self._write_complex_payloads(w)

        self._write_static_validator(w, spec)

        w.write("} // namespace rpc")
        w.write("#endif")

    def _write_file_header(self, w: CodeWriter) -> None:
        w.raw("/*\n")
        w.raw(" * This file is part of Arduino MCU Ecosystem v2.\n")
        w.raw(" * Copyright (C) 2025 Ignacio Santolin and contributors\n")
        w.raw(" *\n")
        w.raw(" * Auto-generated by tools/protocol/generate.py. DO NOT EDIT.\n")
        w.raw(" */\n")

    def _write_constants(self, w: CodeWriter, spec: ProtocolSpec) -> None:
        # Generic Constants mapping (toml key -> cpp type, cpp name)
        mapping = [
            ("protocol_version", "uint8_t", "PROTOCOL_VERSION"),
            ("default_baudrate", "unsigned long", "RPC_DEFAULT_BAUDRATE"),
            ("max_payload_size", "size_t", "MAX_PAYLOAD_SIZE"),
            ("default_safe_baudrate", "unsigned long", "RPC_DEFAULT_SAFE_BAUDRATE"),
            ("max_filepath_length", "size_t", "RPC_MAX_FILEPATH_LENGTH"),
            ("max_datastore_key_length", "size_t", "RPC_MAX_DATASTORE_KEY_LENGTH"),
            ("default_ack_timeout_ms", "unsigned int", "RPC_DEFAULT_ACK_TIMEOUT_MS"),
            ("default_retry_limit", "uint8_t", "RPC_DEFAULT_RETRY_LIMIT"),
            ("max_pending_tx_frames", "uint8_t", "RPC_MAX_PENDING_TX_FRAMES"),
            ("invalid_id_sentinel", "uint16_t", "RPC_INVALID_ID_SENTINEL"),
            ("cmd_flag_compressed", "uint16_t", "RPC_CMD_FLAG_COMPRESSED"),
            ("uint8_mask", "uint8_t", "RPC_UINT8_MASK"),
            ("uint16_max", "uint16_t", "RPC_UINT16_MAX"),
            ("process_default_exit_code", "uint8_t", "RPC_PROCESS_DEFAULT_EXIT_CODE"),
            ("crc32_mask", "uint32_t", "RPC_CRC32_MASK"),
            ("crc_initial", "uint32_t", "RPC_CRC_INITIAL"),
            ("crc_polynomial", "uint32_t", "RPC_CRC_POLYNOMIAL"),
            ("frame_delimiter", "uint8_t", "RPC_FRAME_DELIMITER"),
            ("digital_low", "uint8_t", "RPC_DIGITAL_LOW"),
            ("digital_high", "uint8_t", "RPC_DIGITAL_HIGH"),
            ("rle_escape_byte", "uint8_t", "RPC_RLE_ESCAPE_BYTE"),
            ("rle_min_run_length", "uint8_t", "RPC_RLE_MIN_RUN_LENGTH"),
            ("rle_max_run_length", "uint16_t", "RPC_RLE_MAX_RUN_LENGTH"),
            ("rle_single_escape_marker", "uint8_t", "RPC_RLE_SINGLE_ESCAPE_MARKER"),
            # Category ranges
            ("status_code_min", "uint8_t", "RPC_STATUS_CODE_MIN"),
            ("status_code_max", "uint8_t", "RPC_STATUS_CODE_MAX"),
            ("system_command_min", "uint16_t", "RPC_SYSTEM_COMMAND_MIN"),
            ("system_command_max", "uint16_t", "RPC_SYSTEM_COMMAND_MAX"),
            ("gpio_command_min", "uint16_t", "RPC_GPIO_COMMAND_MIN"),
            ("gpio_command_max", "uint16_t", "RPC_GPIO_COMMAND_MAX"),
            ("console_command_min", "uint16_t", "RPC_CONSOLE_COMMAND_MIN"),
            ("console_command_max", "uint16_t", "RPC_CONSOLE_COMMAND_MAX"),
            ("datastore_command_min", "uint16_t", "RPC_DATASTORE_COMMAND_MIN"),
            ("datastore_command_max", "uint16_t", "RPC_DATASTORE_COMMAND_MAX"),
            ("mailbox_command_min", "uint16_t", "RPC_MAILBOX_COMMAND_MIN"),
            ("mailbox_command_max", "uint16_t", "RPC_MAILBOX_COMMAND_MAX"),
            ("filesystem_command_min", "uint16_t", "RPC_FILESYSTEM_COMMAND_MIN"),
            ("filesystem_command_max", "uint16_t", "RPC_FILESYSTEM_COMMAND_MAX"),
            ("process_command_min", "uint16_t", "RPC_PROCESS_COMMAND_MIN"),
            ("process_command_max", "uint16_t", "RPC_PROCESS_COMMAND_MAX"),
        ]

        for key, ctype, cname in mapping:
            if key in spec.constants:
                val = spec.constants[key]
                w.write(f"constexpr {ctype} {cname} = {val};")
        w.write()

        # Handshake Constants
        hs = spec.handshake
        hs_mapping = [
            ("nonce_length", "unsigned int", "RPC_HANDSHAKE_NONCE_LENGTH"),
            ("tag_length", "unsigned int", "RPC_HANDSHAKE_TAG_LENGTH"),
            ("ack_timeout_min_ms", "uint32_t", "RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS"),
            ("ack_timeout_max_ms", "uint32_t", "RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS"),
            (
                "response_timeout_min_ms",
                "uint32_t",
                "RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS",
            ),
            (
                "response_timeout_max_ms",
                "uint32_t",
                "RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS",
            ),
            ("retry_limit_min", "unsigned int", "RPC_HANDSHAKE_RETRY_LIMIT_MIN"),
            ("retry_limit_max", "unsigned int", "RPC_HANDSHAKE_RETRY_LIMIT_MAX"),
            ("hkdf_output_length", "unsigned int", "RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH"),
            ("nonce_random_bytes", "unsigned int", "RPC_HANDSHAKE_NONCE_RANDOM_BYTES"),
            (
                "nonce_counter_bytes",
                "unsigned int",
                "RPC_HANDSHAKE_NONCE_COUNTER_BYTES",
            ),
        ]
        for key, ctype, cname in hs_mapping:
            if key in hs:
                w.write(f"constexpr {ctype} {cname} = {hs[key]};")

        w.write("constexpr unsigned int RPC_HANDSHAKE_CONFIG_SIZE = 7;")

        # Byte arrays
        if "hkdf_salt" in hs:
            bytes_str = ", ".join(f"0x{ord(c):02X}" for c in hs["hkdf_salt"])
            w.write(f"constexpr uint8_t RPC_HANDSHAKE_HKDF_SALT[] = {{{bytes_str}}};")
            w.write(f"constexpr size_t RPC_HANDSHAKE_HKDF_SALT_LEN = {len(hs['hkdf_salt'])};")

        if "hkdf_info_auth" in hs:
            bytes_str = ", ".join(f"0x{ord(c):02X}" for c in hs["hkdf_info_auth"])
            w.write(f"constexpr uint8_t RPC_HANDSHAKE_HKDF_INFO_AUTH[] = {{{bytes_str}}};")
            w.write(f"constexpr size_t RPC_HANDSHAKE_HKDF_INFO_AUTH_LEN = {len(hs['hkdf_info_auth'])};")
        w.write()

        # Capabilities & Architectures
        for name, val in spec.capabilities.items():
            w.write(f"constexpr uint32_t RPC_CAPABILITY_{name.upper()} = {val};")
        w.write()
        for name, val in spec.architectures.items():
            w.write(f"constexpr uint8_t RPC_ARCH_{name.upper()} = {val};")
        w.write()

    def _write_enums(self, w: CodeWriter, spec: ProtocolSpec) -> None:
        w.write("enum class CompressionType : uint8_t {")
        w.write("    COMPRESSION_NONE = 0,")
        w.write("    COMPRESSION_RLE = 1,")
        w.write("};")
        w.write()

        with w.block("enum class StatusCode : uint8_t {", "};"):
            for s in spec.statuses:
                w.write(f"STATUS_{s.name} = {s.value},")
        w.write()

        with w.block("enum class CommandId : uint16_t {", "};"):
            for c in spec.commands:
                w.write(f"{c.name} = {c.value},")
        w.write()

    def _write_helpers(self, w: CodeWriter, spec: ProtocolSpec) -> None:
        w.write("constexpr uint8_t to_underlying(StatusCode value) {")
        w.write("    return static_cast<uint8_t>(value);")
        w.write("}")
        w.write()
        w.write("constexpr uint16_t to_underlying(CommandId value) {")
        w.write("    return static_cast<uint16_t>(value);")
        w.write("}")
        w.write()

        ack_cmds = [f"(command_id == CommandId::{c.name})" for c in spec.commands if c.requires_ack]
        w.write("constexpr bool requires_ack(CommandId command_id) {")
        if ack_cmds:
            w.write(f"    return {' || '.join(ack_cmds)};")
        else:
            w.write("    return false;")
        w.write("}")
        w.write()

        w.write("constexpr bool requires_ack(uint16_t command_id) {")
        w.write("    return requires_ack(static_cast<CommandId>(command_id));")
        w.write("}")

    def _write_auto_payloads(self, w: CodeWriter, spec: ProtocolSpec) -> None:
        for payload in spec.payloads.values():
            with w.block(f"struct {payload.name} {{", "};"):
                # Fields
                for f in payload.fields:
                    w.write(f"{f.cpp_type} {f.name};")

                w.write(f"static constexpr size_t SIZE = {payload.total_size};")

                # Parse
                with w.block(f"static {payload.name} parse(const uint8_t* data) {{"):
                    if not payload.fields:
                        w.write(f"return {payload.name}{{}};")
                    elif len(payload.fields) == 1 and payload.fields[0].read_func:
                        w.write(f"return {{{payload.fields[0].read_func}(data)}};")
                    elif all(not f.read_func for f in payload.fields):
                        # All bytes, simpler init
                        inits = [f"data[{i}]" for i in range(len(payload.fields))]
                        w.write(f"return {{{', '.join(inits)}}};")
                    else:
                        w.write(f"{payload.name} msg;")
                        offset = 0
                        for f in payload.fields:
                            if f.read_func:
                                w.write(f"msg.{f.name} = {f.read_func}(data + {offset});")
                            else:
                                w.write(f"msg.{f.name} = data[{offset}];")
                            offset += f.size
                        w.write("return msg;")

                # Encode
                with w.block("void encode(uint8_t* data) const {"):
                    offset = 0
                    for f in payload.fields:
                        if f.write_func:
                            w.write(f"{f.write_func}(data + {offset}, {f.name});")
                        else:
                            w.write(f"data[{offset}] = {f.name};")
                        offset += f.size
            w.write()

    def _write_complex_payloads(self, w: CodeWriter) -> None:
        # [SIL-2] Refactored variable-length payload templates.
        # These handle common patterns: Pascal strings (u8 len) and buffers (u16 len).
        complex_payloads = [
            (
                "ConsoleWrite",
                "struct ConsoleWrite { const uint8_t* data; size_t length; "
                "static ConsoleWrite parse(const uint8_t* d, size_t l) { return {d, l}; } };",
            ),
            (
                "DatastoreGet",
                "struct DatastoreGet { etl::string_view key; static DatastoreGet parse(const uint8_t* d) "
                "{ return {etl::string_view(reinterpret_cast<const char*>(d + 1), d[0])}; } };",
            ),
            (
                "DatastoreGetResponse",
                "struct DatastoreGetResponse { const uint8_t* value; uint8_t value_len; "
                "static DatastoreGetResponse parse(const uint8_t* d) { return {d + 1, d[0]}; } };",
            ),
            (
                "DatastorePut",
                "struct DatastorePut { etl::string_view key; const uint8_t* value; uint8_t value_len; "
                "static DatastorePut parse(const uint8_t* d) { uint8_t k = d[0]; return "
                "{etl::string_view(reinterpret_cast<const char*>(d + 1), k), d + 1 + k + 1, d[1 + k]}; } };",
            ),
            (
                "MailboxPush",
                "struct MailboxPush { const uint8_t* data; uint16_t length; "
                "static MailboxPush parse(const uint8_t* d) { return {d + 2, rpc::read_u16_be(d)}; } };",
            ),
            (
                "MailboxReadResponse",
                "struct MailboxReadResponse { const uint8_t* content; uint16_t length; "
                "static MailboxReadResponse parse(const uint8_t* d) { return {d + 2, rpc::read_u16_be(d)}; } };",
            ),
            (
                "FileWrite",
                "struct FileWrite { etl::string_view path; const uint8_t* data; uint16_t data_len; "
                "static FileWrite parse(const uint8_t* d) { uint8_t p = d[0]; return "
                "{etl::string_view(reinterpret_cast<const char*>(d + 1), p), d + 1 + p + 2, "
                "rpc::read_u16_be(d + 1 + p)}; } };",
            ),
            (
                "FileRead",
                "struct FileRead { etl::string_view path; static FileRead parse(const uint8_t* d) "
                "{ return {etl::string_view(reinterpret_cast<const char*>(d + 1), d[0])}; } };",
            ),
            (
                "FileReadResponse",
                "struct FileReadResponse { const uint8_t* content; uint16_t length; "
                "static FileReadResponse parse(const uint8_t* d) { return {d + 2, rpc::read_u16_be(d)}; } };",
            ),
            (
                "FileRemove",
                "struct FileRemove { etl::string_view path; static FileRemove parse(const uint8_t* d) "
                "{ return {etl::string_view(reinterpret_cast<const char*>(d + 1), d[0])}; } };",
            ),
            (
                "ProcessRun",
                "struct ProcessRun { etl::string_view command; static ProcessRun parse(const uint8_t* d, size_t l) "
                "{ return {etl::string_view(reinterpret_cast<const char*>(d), l)}; } };",
            ),
            (
                "ProcessRunAsync",
                "struct ProcessRunAsync { etl::string_view command; "
                "static ProcessRunAsync parse(const uint8_t* d, size_t l) "
                "{ return {etl::string_view(reinterpret_cast<const char*>(d), l)}; } };",
            ),
            (
                "ProcessRunResponse",
                "struct ProcessRunResponse { uint8_t status; const uint8_t* stdout_data; "
                "uint16_t stdout_len; const uint8_t* stderr_data; uint16_t stderr_len; uint8_t exit_code; "
                "static ProcessRunResponse parse(const uint8_t* d) { ProcessRunResponse m; m.status = d[0]; "
                "m.stdout_len = rpc::read_u16_be(d + 1); m.stdout_data = d + 3; "
                "m.stderr_len = rpc::read_u16_be(d + 3 + m.stdout_len); "
                "m.stderr_data = d + 3 + m.stdout_len + 2; "
                "m.exit_code = d[3 + m.stdout_len + 2 + m.stderr_len]; return m; } };",
            ),
            (
                "ProcessPollResponse",
                "struct ProcessPollResponse { uint8_t status; uint8_t exit_code; "
                "const uint8_t* stdout_data; uint16_t stdout_len; const uint8_t* stderr_data; "
                "uint16_t stderr_len; static ProcessPollResponse parse(const uint8_t* d) "
                "{ ProcessPollResponse m; m.status = d[0]; m.exit_code = d[1]; "
                "m.stdout_len = rpc::read_u16_be(d + 2); m.stdout_data = d + 4; "
                "m.stderr_len = rpc::read_u16_be(d + 4 + m.stdout_len); "
                "m.stderr_data = d + 4 + m.stdout_len + 2; return m; } };",
            ),
        ]
        w.write("// --- Complex/Variable Payloads ---")
        for _, code in complex_payloads:
            w.write(code)
        w.write()

    def _write_static_validator(self, w: CodeWriter, spec: ProtocolSpec) -> None:
        with w.block("namespace Payload {"):
            w.write("""
template <typename T>
inline etl::expected<T, rpc::FrameError> parse(const rpc::Frame& frame) {
    if (frame.header.payload_length < T::SIZE) {
        return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);
    }
    return etl::expected<T, rpc::FrameError>(T::parse(frame.payload.data()));
}
""")

            # Manual specializations for variable length payloads
            manual_impls: list[tuple[str, list[str]]] = [
                (
                    "payload::ConsoleWrite",
                    [
                        "return etl::expected<payload::ConsoleWrite, rpc::FrameError>("
                        "payload::ConsoleWrite::parse(frame.payload.data(), frame.header.payload_length));"
                    ],
                ),
                (
                    "payload::ProcessRun",
                    [
                        "return etl::expected<payload::ProcessRun, rpc::FrameError>("
                        "payload::ProcessRun::parse(frame.payload.data(), frame.header.payload_length));"
                    ],
                ),
                (
                    "payload::ProcessRunAsync",
                    [
                        "return etl::expected<payload::ProcessRunAsync, rpc::FrameError>("
                        "payload::ProcessRunAsync::parse(frame.payload.data(), frame.header.payload_length));"
                    ],
                ),
                (
                    "payload::DatastoreGet",
                    [
                        "if (frame.header.payload_length < 1 || "
                        "frame.header.payload_length < (size_t)(frame.payload[0] + 1)) {",
                        "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                        "}",
                        "return etl::expected<payload::DatastoreGet, rpc::FrameError>(payload::DatastoreGet::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::DatastoreGetResponse",
                    [
                        "if (frame.header.payload_length < 1 || "
                        "frame.header.payload_length < (size_t)(frame.payload[0] + 1)) {",
                        "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                        "}",
                        "return etl::expected<payload::DatastoreGetResponse, rpc::FrameError>(payload::DatastoreGetResponse::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::DatastorePut",
                    [
                        "if (frame.header.payload_length < 2) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint8_t k = frame.payload[0];",
                        "if (frame.header.payload_length < (size_t)(k + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint8_t v = frame.payload[k + 1];",
                        "if (frame.header.payload_length < (size_t)(k + v + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "return etl::expected<payload::DatastorePut, rpc::FrameError>(payload::DatastorePut::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::MailboxPush",
                    [
                        "if (frame.header.payload_length < 2) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint16_t l = rpc::read_u16_be(frame.payload.data());",
                        "if (frame.header.payload_length < (size_t)(l + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "return etl::expected<payload::MailboxPush, rpc::FrameError>(payload::MailboxPush::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::MailboxReadResponse",
                    [
                        "if (frame.header.payload_length < 2) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint16_t l = rpc::read_u16_be(frame.payload.data());",
                        "if (frame.header.payload_length < (size_t)(l + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "return etl::expected<payload::MailboxReadResponse, rpc::FrameError>(payload::MailboxReadResponse::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::FileWrite",
                    [
                        "if (frame.header.payload_length < 3) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint8_t p = frame.payload[0];",
                        "if (frame.header.payload_length < (size_t)(p + 3)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint16_t d = rpc::read_u16_be(frame.payload.data() + 1 + p);",
                        "if (frame.header.payload_length < (size_t)(p + d + 3)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "return etl::expected<payload::FileWrite, rpc::FrameError>(payload::FileWrite::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::FileRead",
                    [
                        "if (frame.header.payload_length < 1 || "
                        "frame.header.payload_length < (size_t)(frame.payload[0] + 1)) {",
                        "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                        "}",
                        "return etl::expected<payload::FileRead, rpc::FrameError>(payload::FileRead::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::FileReadResponse",
                    [
                        "if (frame.header.payload_length < 2) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint16_t l = rpc::read_u16_be(frame.payload.data());",
                        "if (frame.header.payload_length < (size_t)(l + 2)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "return etl::expected<payload::FileReadResponse, rpc::FrameError>(payload::FileReadResponse::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::FileRemove",
                    [
                        "if (frame.header.payload_length < 1 || "
                        "frame.header.payload_length < (size_t)(frame.payload[0] + 1)) {",
                        "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                        "}",
                        "return etl::expected<payload::FileRemove, rpc::FrameError>(payload::FileRemove::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::ProcessRunResponse",
                    [
                        "if (frame.header.payload_length < 6) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint16_t o = rpc::read_u16_be(frame.payload.data() + 1);",
                        "if (frame.header.payload_length < (size_t)(o + 5)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint16_t e = rpc::read_u16_be(frame.payload.data() + 3 + o);",
                        "if (frame.header.payload_length < (size_t)(o + e + 6)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "return etl::expected<payload::ProcessRunResponse, rpc::FrameError>(payload::ProcessRunResponse::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
                (
                    "payload::ProcessPollResponse",
                    [
                        "if (frame.header.payload_length < 6) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint16_t o = rpc::read_u16_be(frame.payload.data() + 2);",
                        "if (frame.header.payload_length < (size_t)(o + 6)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "uint16_t e = rpc::read_u16_be(frame.payload.data() + 4 + o);",
                        "if (frame.header.payload_length < (size_t)(o + e + 6)) return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",  # noqa: E501
                        "return etl::expected<payload::ProcessPollResponse, rpc::FrameError>(payload::ProcessPollResponse::parse(frame.payload.data()));",  # noqa: E501
                    ],
                ),
            ]
            for type_name, body_lines in manual_impls:
                header = f"""template <>
inline etl::expected<{type_name}, rpc::FrameError> parse<{type_name}>(const rpc::Frame& frame) {{"""
                with w.block(header, end="}"):
                    for line in body_lines:
                        w.write(line)


class PythonGenerator:
    def generate(self, spec: ProtocolSpec, out: TextIO) -> None:
        w = CodeWriter(out)
        w.raw('"""Auto-generated protocol bindings. Do not edit manually."""\n')
        w.write("from __future__ import annotations")
        w.write()
        w.write("from enum import IntEnum, StrEnum")
        w.write("from typing import Final")
        w.write()

        # MQTT Wildcards
        w.write('MQTT_WILDCARD_SINGLE: Final[str] = "+"')
        w.write('MQTT_WILDCARD_MULTI: Final[str] = "#"')
        w.write()
        w.write()

        # General Constants
        # Map spec key -> py name, type, optional format
        generic_mapping = [
            ("protocol_version", "PROTOCOL_VERSION", "int", None),
            ("default_baudrate", "DEFAULT_BAUDRATE", "int", None),
            ("max_payload_size", "MAX_PAYLOAD_SIZE", "int", None),
            ("default_safe_baudrate", "DEFAULT_SAFE_BAUDRATE", "int", None),
            ("max_filepath_length", "MAX_FILEPATH_LENGTH", "int", None),
            ("max_datastore_key_length", "MAX_DATASTORE_KEY_LENGTH", "int", None),
            ("default_ack_timeout_ms", "DEFAULT_ACK_TIMEOUT_MS", "int", None),
            ("default_retry_limit", "DEFAULT_RETRY_LIMIT", "int", None),
            ("max_pending_tx_frames", "MAX_PENDING_TX_FRAMES", "int", None),
            ("invalid_id_sentinel", "INVALID_ID_SENTINEL", "int", None),
            ("cmd_flag_compressed", "CMD_FLAG_COMPRESSED", "int", None),
            ("uint8_mask", "UINT8_MASK", "int", None),
            ("uint16_max", "UINT16_MAX", "int", None),
            ("process_default_exit_code", "PROCESS_DEFAULT_EXIT_CODE", "int", None),
            ("crc32_mask", "CRC32_MASK", "int", None),
            ("crc_initial", "CRC_INITIAL", "int", None),
            ("crc_polynomial", "CRC_POLYNOMIAL", "int", None),
            ("frame_delimiter", "FRAME_DELIMITER", "bytes", "bytes([{value}])"),
            ("digital_low", "DIGITAL_LOW", "int", None),
            ("digital_high", "DIGITAL_HIGH", "int", None),
            ("rle_escape_byte", "RLE_ESCAPE_BYTE", "int", None),
            ("rle_min_run_length", "RLE_MIN_RUN_LENGTH", "int", None),
            ("rle_max_run_length", "RLE_MAX_RUN_LENGTH", "int", None),
            ("rle_single_escape_marker", "RLE_SINGLE_ESCAPE_MARKER", "int", None),
            ("status_code_min", "STATUS_CODE_MIN", "int", None),
            ("status_code_max", "STATUS_CODE_MAX", "int", None),
            ("system_command_min", "SYSTEM_COMMAND_MIN", "int", None),
            ("system_command_max", "SYSTEM_COMMAND_MAX", "int", None),
            ("gpio_command_min", "GPIO_COMMAND_MIN", "int", None),
            ("gpio_command_max", "GPIO_COMMAND_MAX", "int", None),
            ("console_command_min", "CONSOLE_COMMAND_MIN", "int", None),
            ("console_command_max", "CONSOLE_COMMAND_MAX", "int", None),
            ("datastore_command_min", "DATASTORE_COMMAND_MIN", "int", None),
            ("datastore_command_max", "DATASTORE_COMMAND_MAX", "int", None),
            ("mailbox_command_min", "MAILBOX_COMMAND_MIN", "int", None),
            ("mailbox_command_max", "MAILBOX_COMMAND_MAX", "int", None),
            ("filesystem_command_min", "FILESYSTEM_COMMAND_MIN", "int", None),
            ("filesystem_command_max", "FILESYSTEM_COMMAND_MAX", "int", None),
            ("process_command_min", "PROCESS_COMMAND_MIN", "int", None),
            ("process_command_max", "PROCESS_COMMAND_MAX", "int", None),
        ]

        for key, py_name, py_type, fmt in generic_mapping:
            if key in spec.constants:
                val = spec.constants[key]
                if fmt:
                    val = fmt.format(value=val)
                w.write(f"{py_name}: Final[{py_type}] = {val}")
        w.write()

        # Handshake
        hs_mapping = [
            ("nonce_length", "HANDSHAKE_NONCE_LENGTH", "int"),
            ("tag_length", "HANDSHAKE_TAG_LENGTH", "int"),
            ("ack_timeout_min_ms", "HANDSHAKE_ACK_TIMEOUT_MIN_MS", "int"),
            ("ack_timeout_max_ms", "HANDSHAKE_ACK_TIMEOUT_MAX_MS", "int"),
            ("response_timeout_min_ms", "HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS", "int"),
            ("response_timeout_max_ms", "HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS", "int"),
            ("retry_limit_min", "HANDSHAKE_RETRY_LIMIT_MIN", "int"),
            ("retry_limit_max", "HANDSHAKE_RETRY_LIMIT_MAX", "int"),
            ("hkdf_output_length", "HANDSHAKE_HKDF_OUTPUT_LENGTH", "int"),
            ("nonce_random_bytes", "HANDSHAKE_NONCE_RANDOM_BYTES", "int"),
            ("nonce_counter_bytes", "HANDSHAKE_NONCE_COUNTER_BYTES", "int"),
        ]
        for key, name, ptype in hs_mapping:
            if key in spec.handshake:
                w.write(f"{name}: Final[{ptype}] = {spec.handshake[key]}")

        # String constants
        hs_strs = [
            ("tag_algorithm", "HANDSHAKE_TAG_ALGORITHM"),
            ("tag_description", "HANDSHAKE_TAG_DESCRIPTION"),
            ("hkdf_algorithm", "HANDSHAKE_HKDF_ALGORITHM"),
            ("nonce_format_description", "HANDSHAKE_NONCE_FORMAT_DESCRIPTION"),
        ]
        for key, name in hs_strs:
            if key in spec.handshake:
                self._write_str_const(w, name, str(spec.handshake[key]))

        # Byte constants
        hs_bytes = [
            ("hkdf_salt", "HANDSHAKE_HKDF_SALT"),
            ("hkdf_info_auth", "HANDSHAKE_HKDF_INFO_AUTH"),
        ]
        for key, name in hs_bytes:
            if key in spec.handshake:
                w.write(f'{name}: Final[bytes] = b"{spec.handshake[key]}"')

        w.write("HANDSHAKE_CONFIG_SIZE: Final[int] = 7")
        w.write()
        w.write()

        # Enums and Classes
        with w.block("class CompressionType(IntEnum):", end=None):
            w.write("NONE = 0")
            w.write("RLE = 1")
        w.write()
        w.write()

        # Capabilities
        for name, val in spec.capabilities.items():
            w.write(f"CAPABILITY_{name.upper()}: Final[int] = {val}")
        w.write()
        w.write()

        # Arch
        for name, val in spec.architectures.items():
            w.write(f"ARCH_{name.upper()}: Final[int] = {val}")
        w.write()
        w.write()

        # Status Reasons
        if spec.status_reasons:
            for key in sorted(spec.status_reasons.keys()):
                self._write_str_const(
                    w,
                    f"STATUS_REASON_{str(key).upper()}",
                    str(spec.status_reasons[key]),
                )
            w.write()
            w.write()

        # Status Enum
        with w.block("class Status(IntEnum):", end=None):
            for s in spec.statuses:
                w.write(f"{s.name} = {s.value}  # {s.description}")
        w.write()
        w.write()

        # Command Enum
        ack_only = []
        resp_only = []
        with w.block("class Command(IntEnum):", end=None):
            for c in spec.commands:
                w.write(f"{c.name} = {c.value}")
                if c.requires_ack:
                    ack_only.append(f"Command.{c.name}.value")
                if c.expects_direct_response:
                    resp_only.append(f"Command.{c.name}.value")
        w.write()
        w.write()

        # Sets
        if ack_only:
            w.write("ACK_ONLY_COMMANDS: frozenset[int] = frozenset(")
            with w.indent():
                w.write("{")
                with w.indent():
                    for c in ack_only:
                        w.write(f"{c},")
                w.write("}")
            w.write(")")
            w.write()

        if resp_only:
            w.write("# Commands that expect a direct response without a prior ACK.")
            w.write("# The MCU responds directly with CMD_*_RESP without sending STATUS_ACK first.")
            w.write("RESPONSE_ONLY_COMMANDS: frozenset[int] = frozenset(")
            with w.indent():
                w.write("{")
                with w.indent():
                    for c in resp_only:
                        w.write(f"{c},")
                w.write("}")
            w.write(")")
            w.write()

        # Topics
        with w.block("class Topic(StrEnum):", end=None):
            for t in spec.topics:
                w.write(f"{t['name']} = \"{t['value']}\"  # {t['description']}")
        w.write()
        w.write()

        # Actions (Grouped)
        self._write_actions(w, spec)

        # Subscriptions
        self._write_subscriptions(w, spec)

        # Formats
        formats = {
            "CRC_COVERED_HEADER_FORMAT": ">BHH",
            "CRC_FORMAT": ">I",
            "UINT8_FORMAT": ">B",
            "UINT16_FORMAT": ">H",
            "UINT32_FORMAT": ">I",
            "NONCE_COUNTER_FORMAT": ">Q",
        }
        for name, val in formats.items():
            w.write(f'{name}: Final[str] = "{val}"')
        w.write("CRC_COVERED_HEADER_SIZE: Final[int] = 5")
        w.write("CRC_SIZE: Final[int] = 4")
        w.write("MIN_FRAME_SIZE: Final[int] = 9")
        w.write()
        w.write()

        # Suffixes
        w.write('MQTT_SUFFIX_INCOMING_AVAILABLE: Final[str] = "incoming_available"')
        w.write('MQTT_SUFFIX_OUTGOING_AVAILABLE: Final[str] = "outgoing_available"')
        w.write('MQTT_SUFFIX_RESPONSE: Final[str] = "response"')
        w.write('MQTT_SUFFIX_ERROR: Final[str] = "error"')
        w.write()
        w.write()
        w.write('MQTT_DEFAULT_TOPIC_PREFIX: Final[str] = "br"')

    def _write_str_const(self, w: CodeWriter, name: str, value: str) -> None:
        # Check total line length estimation
        prefix_len = len(f"{name}: Final[str] = ")
        if len(value) + prefix_len > 100:
            w.write(f"{name}: Final[str] = (")
            with w.indent():
                for line in textwrap.wrap(value, 70):  # Wrap earlier
                    w.write(f"{json.dumps(line)}")
            w.write(")")
        else:
            w.write(f"{name}: Final[str] = {json.dumps(value)}")

    def _write_actions(self, w: CodeWriter, spec: ProtocolSpec) -> None:
        grouped: dict[str, list[tuple[str, str, str]]] = {}
        for act in spec.actions:
            raw = act["name"]
            if "_" not in raw:
                continue
            prefix, suffix = raw.split("_", 1)
            grouped.setdefault(prefix, []).append((suffix, act["value"], act["description"]))

        for prefix, items in grouped.items():
            # Special case mapping matching original generator
            cls_name = "DatastoreAction" if prefix == "DATASTORE" else f"{prefix.lower().title()}Action"
            with w.block(f"class {cls_name}(StrEnum):", end=None):
                for suffix, val, desc in items:
                    w.write(f'{suffix} = "{val}"  # {desc}')
            w.write()
            w.write()

    def _write_subscriptions(self, w: CodeWriter, spec: ProtocolSpec) -> None:
        w.write("MQTT_COMMAND_SUBSCRIPTIONS: Final[tuple[tuple[Topic, tuple[str, ...], int], ...]] = (")
        with w.indent():
            for sub in spec.mqtt_subscriptions:
                topic = sub["topic"]
                qos = sub["qos"]
                segments = sub.get("segments", [])

                seg_strs = []
                for s in segments:
                    if s == "+":
                        seg_strs.append("MQTT_WILDCARD_SINGLE")
                    elif s == "#":
                        seg_strs.append("MQTT_WILDCARD_MULTI")
                    else:
                        # Try to map to Enum
                        # Logic duplicated from original for compatibility
                        mapped = False
                        if topic in [
                            "DIGITAL",
                            "ANALOG",
                            "CONSOLE",
                            "DATASTORE",
                            "MAILBOX",
                            "SHELL",
                            "SYSTEM",
                            "FILE",
                        ]:
                            # Simple heuristic to match original output
                            cls_name = "DatastoreAction" if topic == "DATASTORE" else f"{topic.lower().title()}Action"
                            # Check if s matches an action value
                            for act in spec.actions:
                                if act["name"].startswith(f"{topic}_") and act["value"] == s:
                                    suffix = act["name"].split("_", 1)[1]
                                    seg_strs.append(f"{cls_name}.{suffix}.value")
                                    mapped = True
                                    break
                        if not mapped:
                            seg_strs.append(json.dumps(s))

                seg_tuple = f"({', '.join(seg_strs)},)" if seg_strs else "()"
                w.write(f"(Topic.{topic}, {seg_tuple}, {qos}),")
        w.write(")")
        w.write()
        w.write()


# =============================================================================
# 4. Main Entry Point
# =============================================================================


@app.command()
def main(
    spec_path: Annotated[Path, typer.Option("--spec", help="Protocol specification file")],
    cpp: Annotated[Optional[Path], typer.Option("--cpp", help="C++ header output")] = None,
    cpp_structs: Annotated[Optional[Path], typer.Option("--cpp-structs", help="C++ structs output")] = None,
    py: Annotated[Optional[Path], typer.Option("--py", help="Python output")] = None,
) -> None:
    spec = ProtocolSpec.load(spec_path)

    if cpp:
        cpp.parent.mkdir(parents=True, exist_ok=True)
        with cpp.open("w", encoding="utf-8") as f:
            CppGenerator().generate_header(spec, f)
        sys.stdout.write(f"Generated {cpp}\n")

    if cpp_structs:
        cpp_structs.parent.mkdir(parents=True, exist_ok=True)
        with cpp_structs.open("w", encoding="utf-8") as f:
            CppGenerator().generate_structs(spec, f)
        sys.stdout.write(f"Generated {cpp_structs}\n")

    if py:
        py.parent.mkdir(parents=True, exist_ok=True)
        with py.open("w", encoding="utf-8") as f:
            PythonGenerator().generate(spec, f)
        sys.stdout.write(f"Generated {py}\n")


if __name__ == "__main__":
    app()
