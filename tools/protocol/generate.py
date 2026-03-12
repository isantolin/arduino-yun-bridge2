#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Jinja2: Declarative templates for C++ and Python outputs.

Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from __future__ import annotations

import importlib.util
import sys

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

from pathlib import Path  # noqa: E402
from typing import TYPE_CHECKING, Annotated, Optional  # noqa: E402

import typer  # noqa: E402
from jinja2 import Environment, FileSystemLoader  # noqa: E402

# Load ProtocolSpec directly from spec_model.py via importlib.util to avoid
# triggering the protocol package __init__.py, which eagerly imports the
# generated protocol.py module — the very file this generator creates.
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

TEMPLATE_DIR = Path(__file__).parent / "templates"


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

        c = spec.constants
        constants = [
            {"name": "PROTOCOL_VERSION", "type": "uint8_t", "value": c["protocol_version"]},
            {"name": "RPC_DEFAULT_BAUDRATE", "type": "unsigned long", "value": c["default_baudrate"]},
            {"name": "MAX_PAYLOAD_SIZE", "type": "size_t", "value": c["max_payload_size"]},
            {
                "name": "RPC_DEFAULT_SAFE_BAUDRATE",
                "type": "unsigned long",
                "value": c["default_safe_baudrate"],
            },
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
            {"name": "RPC_CMD_FLAG_COMPRESSED", "type": "uint16_t", "value": c["cmd_flag_compressed"]},
            {"name": "RPC_UINT8_MASK", "type": "uint8_t", "value": c["uint8_mask"]},
            {"name": "RPC_UINT16_MAX", "type": "uint16_t", "value": c["uint16_max"]},
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

        # Line-split long complex payload definitions to satisfy linter
        c_write = "struct ConsoleWrite { const uint8_t* data; size_t length; "
        c_write += "static ConsoleWrite parse(const uint8_t* d, size_t l) { return {d, l}; } };"

        ds_get = "struct DatastoreGet { etl::string_view key; static DatastoreGet parse(const uint8_t* d) { "
        ds_get += "return {etl::string_view(reinterpret_cast<const char*>(d + 1), d[0])}; } };"

        ds_get_resp = "struct DatastoreGetResponse { const uint8_t* value; uint8_t value_len; "
        ds_get_resp += "static DatastoreGetResponse parse(const uint8_t* d) { return {d + 1, d[0]}; } };"

        ds_put = "struct DatastorePut { etl::string_view key; const uint8_t* value; uint8_t value_len; "
        ds_put += "static DatastorePut parse(const uint8_t* d) { uint8_t k = d[0]; return "
        ds_put += "{etl::string_view(reinterpret_cast<const char*>(d + 1), k), d + 1 + k + 1, d[1 + k]}; } };"

        m_push = "struct MailboxPush { const uint8_t* data; uint16_t length; "
        m_push += "static MailboxPush parse(const uint8_t* d) { return {d + 2, rpc::read_u16_be(d)}; } };"

        m_read_resp = "struct MailboxReadResponse { const uint8_t* content; uint16_t length; "
        m_read_resp += "static MailboxReadResponse parse(const uint8_t* d) { "
        m_read_resp += "return {d + 2, rpc::read_u16_be(d)}; } };"

        f_write = "struct FileWrite { etl::string_view path; const uint8_t* data; uint16_t data_len; "
        f_write += "static FileWrite parse(const uint8_t* d) { uint8_t p = d[0]; return "
        f_write += "{etl::string_view(reinterpret_cast<const char*>(d + 1), p), d + 1 + p + 2, "
        f_write += "rpc::read_u16_be(d + 1 + p)}; } };"

        f_read = "struct FileRead { etl::string_view path; static FileRead parse(const uint8_t* d) { "
        f_read += "return {etl::string_view(reinterpret_cast<const char*>(d + 1), d[0])}; } };"

        f_read_resp = "struct FileReadResponse { const uint8_t* content; uint16_t length; "
        f_read_resp += "static FileReadResponse parse(const uint8_t* d) { return {d + 2, rpc::read_u16_be(d)}; } };"

        f_remove = "struct FileRemove { etl::string_view path; static FileRemove parse(const uint8_t* d) { "
        f_remove += "return {etl::string_view(reinterpret_cast<const char*>(d + 1), d[0])}; } };"

        p_run = "struct ProcessRun { etl::string_view command; static ProcessRun parse(const uint8_t* d, size_t l) { "
        p_run += "return {etl::string_view(reinterpret_cast<const char*>(d), l)}; } };"

        p_run_async = "struct ProcessRunAsync { etl::string_view command; "
        p_run_async += "static ProcessRunAsync parse(const uint8_t* d, size_t l) { "
        p_run_async += "return {etl::string_view(reinterpret_cast<const char*>(d), l)}; } };"

        p_run_resp = "struct ProcessRunResponse { uint8_t status; const uint8_t* stdout_data; "
        p_run_resp += "uint16_t stdout_len; const uint8_t* stderr_data; uint16_t stderr_len; uint8_t exit_code; "
        p_run_resp += "static ProcessRunResponse parse(const uint8_t* d) { ProcessRunResponse m; m.status = d[0]; "
        p_run_resp += "m.stdout_len = rpc::read_u16_be(d + 1); m.stdout_data = d + 3; "
        p_run_resp += "m.stderr_len = rpc::read_u16_be(d + 3 + m.stdout_len); "
        p_run_resp += "m.stderr_data = d + 3 + m.stdout_len + 2; "
        p_run_resp += "m.exit_code = d[3 + m.stdout_len + 2 + m.stderr_len]; return m; } };"

        p_poll_resp = "struct ProcessPollResponse { uint8_t status; uint8_t exit_code; "
        p_poll_resp += "const uint8_t* stdout_data; uint16_t stdout_len; const uint8_t* stderr_data; "
        p_poll_resp += "uint16_t stderr_len; static ProcessPollResponse parse(const uint8_t* d) { "
        p_poll_resp += "ProcessPollResponse m; m.status = d[0]; m.exit_code = d[1]; "
        p_poll_resp += "m.stdout_len = rpc::read_u16_be(d + 2); m.stdout_data = d + 4; "
        p_poll_resp += "m.stderr_len = rpc::read_u16_be(d + 4 + m.stdout_len); "
        p_poll_resp += "m.stderr_data = d + 4 + m.stdout_len + 2; return m; } };"

        complex_payloads = [
            c_write, ds_get, ds_get_resp, ds_put, m_push, m_read_resp, f_write,
            f_read, f_read_resp, f_remove, p_run, p_run_async, p_run_resp, p_poll_resp
        ]

        manual_specs = [
            {
                "name": "payload::ConsoleWrite",
                "body": [
                    "return etl::expected<payload::ConsoleWrite, rpc::FrameError>(",
                    "    payload::ConsoleWrite::parse(frame.payload.data(), frame.header.payload_length));"
                ],
            },
            {
                "name": "payload::ProcessRun",
                "body": [
                    "return etl::expected<payload::ProcessRun, rpc::FrameError>(",
                    "    payload::ProcessRun::parse(frame.payload.data(), frame.header.payload_length));"
                ],
            },
            {
                "name": "payload::ProcessRunAsync",
                "body": [
                    "return etl::expected<payload::ProcessRunAsync, rpc::FrameError>(",
                    "    payload::ProcessRunAsync::parse(frame.payload.data(), frame.header.payload_length));"
                ],
            },
            {
                "name": "payload::DatastoreGet",
                "body": [
                    "if (frame.header.payload_length < 1 || "
                    "frame.header.payload_length < (size_t)(frame.payload[0] + 1)) {",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "}",
                    "return etl::expected<payload::DatastoreGet, rpc::FrameError>(",
                    "    payload::DatastoreGet::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::DatastoreGetResponse",
                "body": [
                    "if (frame.header.payload_length < 1 || "
                    "frame.header.payload_length < (size_t)(frame.payload[0] + 1)) {",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "}",
                    "return etl::expected<payload::DatastoreGetResponse, rpc::FrameError>(",
                    "    payload::DatastoreGetResponse::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::DatastorePut",
                "body": [
                    "if (frame.header.payload_length < 2) "
                    "return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint8_t k = frame.payload[0];",
                    "if (frame.header.payload_length < (size_t)(k + 2))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint8_t v = frame.payload[k + 1];",
                    "if (frame.header.payload_length < (size_t)(k + v + 2))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "return etl::expected<payload::DatastorePut, rpc::FrameError>(",
                    "    payload::DatastorePut::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::MailboxPush",
                "body": [
                    "if (frame.header.payload_length < 2) "
                    "return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint16_t l = rpc::read_u16_be(frame.payload.data());",
                    "if (frame.header.payload_length < (size_t)(l + 2))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "return etl::expected<payload::MailboxPush, rpc::FrameError>(",
                    "    payload::MailboxPush::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::MailboxReadResponse",
                "body": [
                    "if (frame.header.payload_length < 2) "
                    "return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint16_t l = rpc::read_u16_be(frame.payload.data());",
                    "if (frame.header.payload_length < (size_t)(l + 2))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "return etl::expected<payload::MailboxReadResponse, rpc::FrameError>(",
                    "    payload::MailboxReadResponse::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::FileWrite",
                "body": [
                    "if (frame.header.payload_length < 3) "
                    "return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint8_t p = frame.payload[0];",
                    "if (frame.header.payload_length < (size_t)(p + 3))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint16_t d = rpc::read_u16_be(frame.payload.data() + 1 + p);",
                    "if (frame.header.payload_length < (size_t)(p + d + 3))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "return etl::expected<payload::FileWrite, rpc::FrameError>(",
                    "    payload::FileWrite::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::FileRead",
                "body": [
                    "if (frame.header.payload_length < 1 || "
                    "frame.header.payload_length < (size_t)(frame.payload[0] + 1)) {",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "}",
                    "return etl::expected<payload::FileRead, rpc::FrameError>(",
                    "    payload::FileRead::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::FileReadResponse",
                "body": [
                    "if (frame.header.payload_length < 2) "
                    "return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint16_t l = rpc::read_u16_be(frame.payload.data());",
                    "if (frame.header.payload_length < (size_t)(l + 2))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "return etl::expected<payload::FileReadResponse, rpc::FrameError>(",
                    "    payload::FileReadResponse::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::FileRemove",
                "body": [
                    "if (frame.header.payload_length < 1 || "
                    "frame.header.payload_length < (size_t)(frame.payload[0] + 1)) {",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "}",
                    "return etl::expected<payload::FileRemove, rpc::FrameError>(",
                    "    payload::FileRemove::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::ProcessRunResponse",
                "body": [
                    "if (frame.header.payload_length < 6) "
                    "return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint16_t o = rpc::read_u16_be(frame.payload.data() + 1);",
                    "if (frame.header.payload_length < (size_t)(o + 5))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint16_t e = rpc::read_u16_be(frame.payload.data() + 3 + o);",
                    "if (frame.header.payload_length < (size_t)(o + e + 6))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "return etl::expected<payload::ProcessRunResponse, rpc::FrameError>(",
                    "    payload::ProcessRunResponse::parse(frame.payload.data()));"
                ],
            },
            {
                "name": "payload::ProcessPollResponse",
                "body": [
                    "if (frame.header.payload_length < 6) "
                    "return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint16_t o = rpc::read_u16_be(frame.payload.data() + 2);",
                    "if (frame.header.payload_length < (size_t)(o + 6))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "uint16_t e = rpc::read_u16_be(frame.payload.data() + 4 + o);",
                    "if (frame.header.payload_length < (size_t)(o + e + 6))",
                    "    return etl::unexpected<rpc::FrameError>(rpc::FrameError::MALFORMED);",
                    "return etl::expected<payload::ProcessPollResponse, rpc::FrameError>(",
                    "    payload::ProcessPollResponse::parse(frame.payload.data()));"
                ],
            },
        ]

        render = template.render(
            payloads=spec.payloads.values(),
            complex_payloads=complex_payloads,
            manual_parse_specializations=manual_specs,
        )
        out_path.write_text(render, encoding="utf-8")

    def generate_python(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("protocol.py.j2")

        c = spec.constants
        constants = [
            {"name": "PROTOCOL_VERSION", "type": "int", "value": c["protocol_version"]},
            {"name": "DEFAULT_BAUDRATE", "type": "int", "value": c["default_baudrate"]},
            {"name": "MAX_PAYLOAD_SIZE", "type": "int", "value": c["max_payload_size"]},
            {"name": "DEFAULT_SAFE_BAUDRATE", "type": "int", "value": c["default_safe_baudrate"]},
            {"name": "MAX_FILEPATH_LENGTH", "type": "int", "value": c["max_filepath_length"]},
            {"name": "MAX_DATASTORE_KEY_LENGTH", "type": "int", "value": c["max_datastore_key_length"]},
            {"name": "DEFAULT_ACK_TIMEOUT_MS", "type": "int", "value": c["default_ack_timeout_ms"]},
            {"name": "DEFAULT_RETRY_LIMIT", "type": "int", "value": c["default_retry_limit"]},
            {"name": "MAX_PENDING_TX_FRAMES", "type": "int", "value": c["max_pending_tx_frames"]},
            {"name": "INVALID_ID_SENTINEL", "type": "int", "value": c["invalid_id_sentinel"]},
            {"name": "CMD_FLAG_COMPRESSED", "type": "int", "value": c["cmd_flag_compressed"]},
            {"name": "UINT8_MASK", "type": "int", "value": c["uint8_mask"]},
            {"name": "UINT16_MAX", "type": "int", "value": c["uint16_max"]},
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
            action_map.setdefault(prefix, []).append(
                {
                    "name": suffix,
                    "value": act["value"],
                    "description": act["description"],
                }
            )

        for prefix, items in action_map.items():
            cls_name = (
                "DatastoreAction"
                if prefix == "DATASTORE"
                else f"{prefix.lower().title()}Action"
            )
            grouped_actions.append({"class_name": cls_name, "action_items": items})

        # Process subscriptions
        subscriptions = []
        for sub in spec.mqtt_subscriptions:
            segments = []
            topic_str = sub["topic"]
            for s in sub.get("segments", []):
                if s == "+":
                    segments.append("MQTT_WILDCARD_SINGLE")
                elif s == "#":
                    segments.append("MQTT_WILDCARD_MULTI")
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
                                segments.append(f"{c_name}.{sfx}.value")
                                mapped = True
                                break
                    if not mapped:
                        segments.append(f'"{s}"')

            subscriptions.append(
                {
                    "topic": topic_str,
                    "qos": sub["qos"],
                    "segments_tuple": f"({', '.join(segments)},)" if segments else "()",
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
        )
        out_path.write_text(render, encoding="utf-8")


@app.command()
def main(
    spec_path: Annotated[Path, typer.Option("--spec", help="Protocol specification file")],
    cpp: Annotated[Optional[Path], typer.Option("--cpp", help="C++ header output")] = None,
    cpp_structs: Annotated[
        Optional[Path], typer.Option("--cpp-structs", help="C++ structs output")
    ] = None,
    py: Annotated[Optional[Path], typer.Option("--py", help="Python output")] = None,
    py_client: Annotated[Optional[Path], typer.Option("--py-client", help="Python client output")] = None,
) -> None:
    spec = ProtocolSpec.load(spec_path)
    gen = JinjaGenerator()

    if cpp:
        cpp.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_header(spec, cpp)
        sys.stderr.write(f"Generated {cpp}\n")

    if cpp_structs:
        cpp_structs.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_structs(spec, cpp_structs)
        sys.stderr.write(f"Generated {cpp_structs}\n")

    if py:
        py.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_python(spec, py)
        sys.stderr.write(f"Generated {py}\n")

    if py_client:
        py_client.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_python_client(spec, py_client)
        sys.stderr.write(f"Generated {py_client}\n")


if __name__ == "__main__":
    app()
