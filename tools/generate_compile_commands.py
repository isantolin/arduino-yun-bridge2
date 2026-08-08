#!/usr/bin/env python3
"""Generate compile_commands.json for C++ tooling and LSP server configuration."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
root_str = str(ROOT)

cmd_str = (
    "/usr/bin/g++ -std=c++17 -O2 -g -Wall -Wextra -Werror "
    "-DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 "
    "-DARDUINO_STUB_CUSTOM_SERIAL=1 -DNUM_DIGITAL_PINS=20 -DNUM_ANALOG_INPUTS=6 "
    "-DWOLFSSL_USER_SETTINGS -DETL_NO_STL -Imcubridge-library-arduino/src "
    "-Imcubridge-library-arduino/src/config -Itools/arduino_stub/include "
    "-I.tmp_tests/arduino_libs/Embedded_Template_Library "
    "-I.tmp_tests/arduino_libs/Embedded_Template_Library/include "
    "-I.tmp_tests/arduino_libs/Embedded_Template_Library/arduino "
    "-I.tmp_tests/arduino_libs/wolfSSL -I.tmp_tests/arduino_libs/PacketSerial "
    "-I.tmp_tests/arduino_libs/PacketSerial/src "
    "-I.dummy_libs/Embedded_Template_Library "
    "-I.dummy_libs/Embedded_Template_Library/include "
    "-I.dummy_libs/Embedded_Template_Library/arduino -I.dummy_libs/wolfSSL "
    "-I.dummy_libs/PacketSerial -I.dummy_libs/PacketSerial/src "
    "-c mcubridge-library-arduino/src/Bridge.cpp -o /dev/null"
)

commands = [
    {
        "directory": root_str,
        "command": cmd_str,
        "file": str(ROOT / "mcubridge-library-arduino" / "src" / "Bridge.cpp"),
    },
    {
        "directory": root_str,
        "command": cmd_str,
        "file": str(ROOT / "mcubridge-library-arduino" / "src" / "Bridge.h"),
    },
]

(ROOT / "compile_commands.json").write_text(json.dumps(commands, indent=2), encoding="utf-8")
print("Generated compile_commands.json with root:", ROOT)
