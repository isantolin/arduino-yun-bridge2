#!/usr/bin/env python3
"""Generate compile_commands.json for C++ tooling and LSP analysis."""

from __future__ import annotations

import json
from pathlib import Path
import typer

ROOT = Path(__file__).resolve().parent.parent
app = typer.Typer(help="Generate compile_commands.json for Arduino MCU C++ tooling.", add_completion=False)

cmd_str = (
    "/usr/bin/g++ -std=c++17 -O2 -g -Wall -Wextra -Werror "
    "-DBRIDGE_HOST_TEST=1 -DBRIDGE_ENABLE_TEST_INTERFACE=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 "
    "-DARDUINO_STUB_CUSTOM_SERIAL=1 -DNUM_DIGITAL_PINS=20 -DNUM_ANALOG_INPUTS=6 "
    "-DWOLFSSL_USER_SETTINGS -DETL_NO_STL -Imcubridge-library-arduino/src "
    "-Imcubridge-library-arduino/src/config -Itools/arduino_stub/include "
    "-Imcubridge-library-arduino/tests -Imcubridge-library-arduino/tests/Unity/src -I.dummy_libs/Unity/src "
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


@app.command()
def main(
    output: Path = ROOT / "compile_commands.json",
) -> None:
    """Generate compile_commands.json file for compile database tooling."""
    cpp_files = (
        list((ROOT / "mcubridge-library-arduino/src").rglob("*.cpp"))
        + list((ROOT / "mcubridge-library-arduino/src").rglob("*.h"))
        + list((ROOT / "mcubridge-library-arduino/tests").rglob("*.cpp"))
        + list((ROOT / "mcubridge-library-arduino/tests").rglob("*.h"))
    )

    commands = [
        {
            "directory": str(ROOT),
            "command": cmd_str.replace("mcubridge-library-arduino/src/Bridge.cpp", str(p.relative_to(ROOT))),
            "file": str(p),
        }
        for p in cpp_files
    ]

    output.write_text(json.dumps(commands, indent=2), encoding="utf-8")
    print(f"Generated compile_commands.json at {output}")


if __name__ == "__main__":
    app()
