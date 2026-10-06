"""Resolve board metadata from the selected Arduino core without local defaults."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

HOST_TEST_FQBN = "arduino:avr:uno"
REQUIRED_PIN_MACROS = (
    "LED_BUILTIN",
    "NUM_DIGITAL_PINS",
    "NUM_ANALOG_INPUTS",
    "PIN_SPI_SS",
    "PIN_SPI_MOSI",
    "PIN_SPI_MISO",
    "PIN_SPI_SCK",
    "PIN_WIRE_SDA",
    "PIN_WIRE_SCL",
)


@dataclass(frozen=True, slots=True)
class ArduinoCoreMetadata:
    fqbn: str
    mcu: str
    frequency_hz: int
    led_builtin: int
    digital_pins: int
    analog_inputs: int
    spi_ss: int
    spi_mosi: int
    spi_miso: int
    spi_sck: int
    i2c_sda: int
    i2c_scl: int


def _read_variant_macros(header: Path, seen: set[Path] | None = None) -> dict[str, str]:
    visited = set() if seen is None else seen
    resolved_header = header.resolve()
    if resolved_header in visited:
        return {}
    visited.add(resolved_header)
    content = resolved_header.read_text(encoding="utf-8")
    macros: dict[str, str] = {}

    for line in content.splitlines():
        include = re.match(r'^\s*#\s*include\s*"([^"]+)"', line)
        if include:
            include_path = resolved_header.parent / include.group(1)
            if include_path.is_file():
                macros.update(_read_variant_macros(include_path, visited))
            continue
        definition = re.match(r"^\s*#\s*define\s+([A-Z][A-Z0-9_]*)\s+(.+?)\s*$", line)
        if definition and definition.group(1) in REQUIRED_PIN_MACROS:
            macros[definition.group(1)] = definition.group(2).split("//", maxsplit=1)[0].strip()
    return macros


def _parse_integer(value: str, name: str) -> int:
    literal = re.fullmatch(r"\(?\s*(0[xX][0-9A-Fa-f]+|[0-9]+)\s*\)?[uUlL]*", value)
    if literal is None:
        raise ValueError(f"Arduino core metadata {name} is not an integer literal: {value!r}")
    return int(literal.group(1), 0)


def metadata_from_properties(fqbn: str, output: str) -> ArduinoCoreMetadata:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()
    required_properties = ("build.f_cpu", "build.mcu", "build.variant.path")
    missing_properties = [name for name in required_properties if name not in properties]
    if missing_properties:
        raise ValueError(f"Arduino CLI omitted required build properties: {', '.join(missing_properties)}")
    if not properties["build.mcu"]:
        raise ValueError("Arduino CLI returned an empty build.mcu property")

    variant_header = Path(properties["build.variant.path"]) / "pins_arduino.h"
    if not variant_header.is_file():
        raise ValueError(f"Arduino core variant header not found: {variant_header}")
    macros = _read_variant_macros(variant_header)
    missing_macros = [name for name in REQUIRED_PIN_MACROS if name not in macros]
    if missing_macros:
        raise ValueError(
            f"Arduino core variant {variant_header} omits required pin metadata: {', '.join(missing_macros)}"
        )

    return ArduinoCoreMetadata(
        fqbn=fqbn,
        mcu=properties["build.mcu"],
        frequency_hz=_parse_integer(properties["build.f_cpu"], "build.f_cpu"),
        led_builtin=_parse_integer(macros["LED_BUILTIN"], "LED_BUILTIN"),
        digital_pins=_parse_integer(macros["NUM_DIGITAL_PINS"], "NUM_DIGITAL_PINS"),
        analog_inputs=_parse_integer(macros["NUM_ANALOG_INPUTS"], "NUM_ANALOG_INPUTS"),
        spi_ss=_parse_integer(macros["PIN_SPI_SS"], "PIN_SPI_SS"),
        spi_mosi=_parse_integer(macros["PIN_SPI_MOSI"], "PIN_SPI_MOSI"),
        spi_miso=_parse_integer(macros["PIN_SPI_MISO"], "PIN_SPI_MISO"),
        spi_sck=_parse_integer(macros["PIN_SPI_SCK"], "PIN_SPI_SCK"),
        i2c_sda=_parse_integer(macros["PIN_WIRE_SDA"], "PIN_WIRE_SDA"),
        i2c_scl=_parse_integer(macros["PIN_WIRE_SCL"], "PIN_WIRE_SCL"),
    )


def resolve_core_metadata(fqbn: str) -> ArduinoCoreMetadata:
    try:
        result = subprocess.run(
            ["arduino-cli", "board", "details", "--show-properties", "--fqbn", fqbn],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as exc:
        raise RuntimeError(f"Could not run arduino-cli for {fqbn}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"arduino-cli could not resolve {fqbn}: {result.stderr.strip()}")
    return metadata_from_properties(fqbn, result.stdout)


def metadata_header(metadata: ArduinoCoreMetadata) -> str:
    macros = (
        ("F_CPU", metadata.frequency_hz),
        ("LED_BUILTIN", metadata.led_builtin),
        ("NUM_DIGITAL_PINS", metadata.digital_pins),
        ("NUM_ANALOG_INPUTS", metadata.analog_inputs),
        ("PIN_SPI_SS", metadata.spi_ss),
        ("PIN_SPI_MOSI", metadata.spi_mosi),
        ("PIN_SPI_MISO", metadata.spi_miso),
        ("PIN_SPI_SCK", metadata.spi_sck),
        ("PIN_WIRE_SDA", metadata.i2c_sda),
        ("PIN_WIRE_SCL", metadata.i2c_scl),
    )
    lines = [
        "#pragma once",
        f"// Generated from {metadata.fqbn} Arduino core metadata; do not edit.",
        *(f"#define {name} {value}{'L' if name == 'F_CPU' else ''}" for name, value in macros),
        "",
    ]
    return "\n".join(lines)


def write_header(metadata: ArduinoCoreMetadata, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(metadata_header(metadata), encoding="utf-8")


def write_metadata_json(metadata: ArduinoCoreMetadata, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(metadata), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_metadata_json(path: Path) -> ArduinoCoreMetadata:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read Arduino core metadata from {path}: {exc}") from exc
    expected_fields = {field.name for field in fields(ArduinoCoreMetadata)}
    if not isinstance(data, dict) or set(data) != expected_fields:
        raise ValueError(f"Invalid Arduino core metadata shape in {path}")
    string_fields = {"fqbn", "mcu"}
    if any(not isinstance(data[name], str) for name in string_fields) or any(
        type(data[name]) is not int for name in expected_fields - string_fields
    ):
        raise ValueError(f"Invalid Arduino core metadata values in {path}")
    return ArduinoCoreMetadata(**data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--fqbn")
    target.add_argument("--host-test", action="store_true")
    parser.add_argument("--write-header", type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    if args.write_header is None and args.json_output is None:
        parser.error("at least one of --write-header or --json-output is required")

    fqbn = HOST_TEST_FQBN if args.host_test else args.fqbn
    try:
        metadata = resolve_core_metadata(fqbn)
        if args.write_header is not None:
            write_header(metadata, args.write_header)
        if args.json_output is not None:
            write_metadata_json(metadata, args.json_output)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Arduino core metadata error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
