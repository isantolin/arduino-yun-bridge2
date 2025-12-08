#!/usr/bin/env python3
"""Generate protocol bindings for Python and C++ from a shared spec."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import struct
import textwrap
from pathlib import Path
from collections.abc import Iterable

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "tools/protocol/spec.toml"
PYTHON_OUTPUT = REPO_ROOT / "openwrt-yun-bridge/yunbridge/rpc/protocol.py"
CPP_OUTPUT = REPO_ROOT / "openwrt-library-arduino/src/protocol/rpc_protocol.h"

LICENSE_HEADER = textwrap.dedent(
    """\
    This file is part of Arduino Yun Ecosystem v2.

    Copyright (C) 2025 Ignacio Santolin and contributors

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
    """
)


def _license_python_comment() -> str:
    lines: list[str] = []
    for line in LICENSE_HEADER.strip().splitlines():
        lines.append(f"# {line}" if line else "#")
    return "\n".join(lines)


PY_LICENSE_COMMENT = _license_python_comment()


@dataclass(slots=True)
class Status:
    name: str
    value: int
    description: str


@dataclass(slots=True)
class Command:
    name: str
    value: int
    category: str
    direction: str


@dataclass(slots=True)
class Handshake:
    nonce_length: int
    tag_length: int
    tag_algorithm: str
    tag_description: str
    config_format: str
    config_description: str
    ack_timeout_min_ms: int
    ack_timeout_max_ms: int
    response_timeout_min_ms: int
    response_timeout_max_ms: int
    retry_limit_min: int
    retry_limit_max: int


def _format_hex(value: int, width: int = 2) -> str:
    return f"0x{value:0{width}X}"


def load_spec(
    path: Path,
) -> tuple[
    dict[str, int],
    dict[str, str],
    Handshake,
    list[Status],
    list[Command],
]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    constants = {
        "PROTOCOL_VERSION": int(data["constants"]["protocol_version"]),
        "MAX_PAYLOAD_SIZE": int(data["constants"]["max_payload_size"]),
        "RPC_BUFFER_SIZE": int(data["constants"]["rpc_buffer_size"]),
    }
    data_formats = {
        "DATASTORE_KEY_LEN_FORMAT": data["data_formats"][
            "datastore_key_len_format"
        ],
        "DATASTORE_VALUE_LEN_FORMAT": data["data_formats"][
            "datastore_value_len_format"
        ],
        "CRC_COVERED_HEADER_FORMAT": data["data_formats"][
            "crc_covered_header_format"
        ],
        "CRC_FORMAT": data["data_formats"]["crc_format"],
    }
    handshake_data = data.get("handshake", {})
    handshake = Handshake(
        nonce_length=int(handshake_data.get("nonce_length", 0)),
        tag_length=int(handshake_data.get("tag_length", 0)),
        tag_algorithm=str(handshake_data.get("tag_algorithm", "")),
        tag_description=str(
            handshake_data.get(
                "tag_description",
                "",
            )
        ),
        config_format=str(handshake_data.get("config_format", "")),
        config_description=str(
            handshake_data.get(
                "config_description",
                "",
            )
        ),
        ack_timeout_min_ms=int(handshake_data.get("ack_timeout_min_ms", 0)),
        ack_timeout_max_ms=int(handshake_data.get("ack_timeout_max_ms", 0)),
        response_timeout_min_ms=int(
            handshake_data.get("response_timeout_min_ms", 0)
        ),
        response_timeout_max_ms=int(
            handshake_data.get("response_timeout_max_ms", 0)
        ),
        retry_limit_min=int(handshake_data.get("retry_limit_min", 0)),
        retry_limit_max=int(handshake_data.get("retry_limit_max", 0)),
    )
    statuses = [
        Status(
            name=entry["name"],
            value=int(entry["value"]),
            description=entry.get("description", ""),
        )
        for entry in data.get("statuses", [])
    ]
    commands = [
        Command(
            name=entry["name"],
            value=int(entry["value"]),
            category=entry.get("category", "uncategorized"),
            direction=entry.get("direction", "unspecified"),
        )
        for entry in data.get("commands", [])
    ]
    return constants, data_formats, handshake, statuses, commands


def generate_python(
    constants: dict[str, int],
    data_formats: dict[str, str],
    handshake: Handshake,
    statuses: list[Status],
    commands: list[Command],
) -> str:
    lines: list[str] = [
        '"""Auto-generated protocol bindings. Do not edit manually.',
        '',
        (
            'Generated by tools/protocol/generate.py '
            'from tools/protocol/spec.toml.'
        ),
        '"""',
        '',
        'from __future__ import annotations',
        '',
        'import struct',
        'from enum import IntEnum',
        'from typing import Final',
        '',
    ]

    lines.extend(PY_LICENSE_COMMENT.splitlines())
    lines.append('')

    lines.append(
        'PROTOCOL_VERSION: Final[int] = '
        f'{_format_hex(constants["PROTOCOL_VERSION"])}'
    )
    lines.append(
        'MAX_PAYLOAD_SIZE: Final[int] = '
        f'{constants["MAX_PAYLOAD_SIZE"]}'
    )
    lines.append(
        'RPC_BUFFER_SIZE: Final[int] = '
        f'{constants["RPC_BUFFER_SIZE"]}'
    )
    lines.append('')

    lines.append(
        'CRC_COVERED_HEADER_FORMAT: str = '
        f'"{data_formats["CRC_COVERED_HEADER_FORMAT"]}"'
    )
    lines.append('CRC_COVERED_HEADER_SIZE: int = struct.calcsize(')
    lines.append('    CRC_COVERED_HEADER_FORMAT')
    lines.append(')')
    lines.append('')

    lines.append(
        'CRC_FORMAT: str = '
        f'"{data_formats["CRC_FORMAT"]}"'
    )
    lines.append('CRC_SIZE: int = struct.calcsize(CRC_FORMAT)')
    lines.append('CRC_BITS: int = CRC_SIZE * 8')
    lines.append('')

    lines.append('MIN_FRAME_SIZE: int = CRC_COVERED_HEADER_SIZE + CRC_SIZE')
    lines.append('')

    lines.append(
        'DATASTORE_KEY_LEN_FORMAT: str = '
        f'"{data_formats["DATASTORE_KEY_LEN_FORMAT"]}"'
    )
    lines.append('DATASTORE_KEY_LEN_SIZE: int = struct.calcsize(')
    lines.append('    DATASTORE_KEY_LEN_FORMAT')
    lines.append(')')
    lines.append('')

    lines.append(
        'DATASTORE_VALUE_LEN_FORMAT: str = '
        f'"{data_formats["DATASTORE_VALUE_LEN_FORMAT"]}"'
    )
    lines.append('DATASTORE_VALUE_LEN_SIZE: int = struct.calcsize(')
    lines.append('    DATASTORE_VALUE_LEN_FORMAT')
    lines.append(')')
    lines.append('')

    lines.append(
        'HANDSHAKE_NONCE_LENGTH: Final[int] = '
        f'{handshake.nonce_length}'
    )
    lines.append(
        'HANDSHAKE_TAG_LENGTH: Final[int] = '
        f'{handshake.tag_length}'
    )
    lines.append(
        'HANDSHAKE_TAG_ALGORITHM: Final[str] = '
        f'{handshake.tag_algorithm!r}'
    )
    lines.append(
        'HANDSHAKE_TAG_DESCRIPTION: Final[str] = '
        f'{handshake.tag_description!r}'
    )
    lines.append(
        'HANDSHAKE_CONFIG_FORMAT: Final[str] = '
        f'{handshake.config_format!r}'
    )
    lines.append('HANDSHAKE_CONFIG_SIZE: Final[int] = struct.calcsize(')
    lines.append('    HANDSHAKE_CONFIG_FORMAT')
    lines.append(')')
    lines.append(
        'HANDSHAKE_CONFIG_DESCRIPTION: Final[str] = '
        f'{handshake.config_description!r}'
    )
    lines.append(
        'HANDSHAKE_ACK_TIMEOUT_MIN_MS: Final[int] = '
        f'{handshake.ack_timeout_min_ms}'
    )
    lines.append(
        'HANDSHAKE_ACK_TIMEOUT_MAX_MS: Final[int] = '
        f'{handshake.ack_timeout_max_ms}'
    )
    lines.append(
        'HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS: Final[int] = '
        f'{handshake.response_timeout_min_ms}'
    )
    lines.append(
        'HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS: Final[int] = '
        f'{handshake.response_timeout_max_ms}'
    )
    lines.append(
        'HANDSHAKE_RETRY_LIMIT_MIN: Final[int] = '
        f'{handshake.retry_limit_min}'
    )
    lines.append(
        'HANDSHAKE_RETRY_LIMIT_MAX: Final[int] = '
        f'{handshake.retry_limit_max}'
    )
    lines.append('')

    lines.append('')
    lines.append('class Status(IntEnum):')
    lines.append('    """Status codes shared between MCU and MPU."""')
    if statuses:
        for status in statuses:
            suffix = f'  # {status.description}' if status.description else ''
            lines.append(
                f'    {status.name} = {_format_hex(status.value)}{suffix}'
            )
    else:
        lines.append('    pass')
    lines.append('')
    lines.append('')
    lines.append('class Command(IntEnum):')
    lines.append('    """Command identifiers shared between MCU and MPU."""')
    if commands:
        for command in commands:
            lines.append(
                f'    {command.name} = {_format_hex(command.value)}'
            )
    else:
        lines.append('    pass')

    return "\n".join(lines) + "\n"


def _categorize(commands: Iterable[Command]) -> dict[str, list[Command]]:
    categories: dict[str, list[Command]] = {}
    for command in commands:
        categories.setdefault(command.category, []).append(command)
    return categories


def generate_cpp(
    constants: dict[str, int],
    data_formats: dict[str, str],
    handshake: Handshake,
    statuses: list[Status],
    commands: list[Command],
) -> str:
    crc_trailer_size = struct.calcsize(data_formats["CRC_FORMAT"])
    handshake_config_size = (
        struct.calcsize(handshake.config_format)
        if handshake.config_format
        else 0
    )
    categories = _categorize(commands)
    status_lines = "\n".join(
        f"#define STATUS_{status.name} {_format_hex(status.value)}"
        for status in statuses
    )

    def _escape_cpp(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    sections: list[str] = []
    for category, entries in categories.items():
        header = f"// {category.replace('_', ' ').title()}"
        body = "\n".join(
            f"#define {command.name} {_format_hex(command.value)}"
            for command in entries
        )
        sections.append(f"{header}\n{body}")

    handshake_struct_block = ""
    if handshake_config_size:
        handshake_struct_block = textwrap.dedent(
            """\
            struct RpcHandshakeTimingConfigWire {
                uint16_t ack_timeout_ms;
                uint8_t retry_limit;
                uint32_t response_timeout_ms;
            } __attribute__((packed));

            static_assert(
                sizeof(RpcHandshakeTimingConfigWire)
                == RPC_HANDSHAKE_CONFIG_SIZE,
                "Handshake config size mismatch with spec.toml"
            );

            """
        )

    cpp_template = textwrap.dedent(
        """\
        /*
        {license_block}
         */
        #ifndef RPC_PROTOCOL_H
        #define RPC_PROTOCOL_H

        #include <cstddef>

        #include "rpc_frame.h"

        static_assert(
            rpc::PROTOCOL_VERSION == {protocol_version},
            "Protocol version mismatch with spec.toml"
        );
        static_assert(
            rpc::MAX_PAYLOAD_SIZE == {max_payload},
            "Max payload size mismatch with spec.toml"
        );
        static_assert(
            rpc::CRC_TRAILER_SIZE == {crc_trailer},
            "CRC trailer size mismatch with spec.toml"
        );

        constexpr unsigned int RPC_BUFFER_SIZE = {rpc_buffer};
        constexpr std::size_t RPC_HANDSHAKE_NONCE_LENGTH = {nonce_len}u;
        constexpr std::size_t RPC_HANDSHAKE_TAG_LENGTH = {tag_len}u;
        constexpr const char RPC_HANDSHAKE_TAG_ALGORITHM[] =
            "{tag_algo}";
        constexpr const char RPC_HANDSHAKE_TAG_DESCRIPTION[] =
            "{tag_desc}";
        constexpr const char RPC_HANDSHAKE_CONFIG_FORMAT[] =
            "{config_format}";
        constexpr const char RPC_HANDSHAKE_CONFIG_DESCRIPTION[] =
            "{config_desc}";
        constexpr std::size_t RPC_HANDSHAKE_CONFIG_SIZE = {config_size}u;
        constexpr unsigned int RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS =
            {ack_min};
        constexpr unsigned int RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS =
            {ack_max};
        constexpr unsigned int RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS =
            {resp_min};
        constexpr unsigned int RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS =
            {resp_max};
        constexpr unsigned int RPC_HANDSHAKE_RETRY_LIMIT_MIN =
            {retry_min};
        constexpr unsigned int RPC_HANDSHAKE_RETRY_LIMIT_MAX =
            {retry_max};

        {handshake_struct_block}

        // Status Codes
        {status_definitions}

        // Command Identifiers
        {command_definitions}

        #endif  // RPC_PROTOCOL_H
        """
    )

    return cpp_template.format(
        license_block=textwrap.indent(LICENSE_HEADER.strip(), " * "),
        protocol_version=_format_hex(constants["PROTOCOL_VERSION"]),
        max_payload=constants["MAX_PAYLOAD_SIZE"],
        crc_trailer=crc_trailer_size,
        rpc_buffer=constants["RPC_BUFFER_SIZE"],
        nonce_len=handshake.nonce_length,
        tag_len=handshake.tag_length,
        tag_algo=_escape_cpp(handshake.tag_algorithm),
        tag_desc=_escape_cpp(handshake.tag_description),
        config_format=_escape_cpp(handshake.config_format),
        config_desc=_escape_cpp(handshake.config_description),
        config_size=handshake_config_size,
        ack_min=handshake.ack_timeout_min_ms,
        ack_max=handshake.ack_timeout_max_ms,
        resp_min=handshake.response_timeout_min_ms,
        resp_max=handshake.response_timeout_max_ms,
        retry_min=handshake.retry_limit_min,
        retry_max=handshake.retry_limit_max,
        handshake_struct_block=handshake_struct_block.rstrip(),
        status_definitions=status_lines,
        command_definitions="\n\n".join(sections),
    ).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate protocol bindings from spec."
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=SPEC_PATH,
        help="Path to the protocol specification (TOML)",
    )
    parser.add_argument(
        "--python-output",
        type=Path,
        default=PYTHON_OUTPUT,
        help="Destination for the generated Python module",
    )
    parser.add_argument(
        "--cpp-output",
        type=Path,
        default=CPP_OUTPUT,
        help="Destination for the generated C++ header",
    )
    args = parser.parse_args()

    constants, data_formats, handshake, statuses, commands = load_spec(
        args.spec
    )

    args.python_output.write_text(
        generate_python(
            constants,
            data_formats,
            handshake,
            statuses,
            commands,
        ),
        encoding="utf-8",
    )
    args.cpp_output.write_text(
        generate_cpp(
            constants,
            data_formats,
            handshake,
            statuses,
            commands,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
