"""Resolve board metadata from the selected Arduino core without local defaults."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Annotated, cast

import parse
import typer

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


_INCLUDE_PARSER = parse.compile('include "{header}"')


def _read_variant_macros(header: Path, seen: set[Path] | None = None) -> dict[str, str]:
    visited: set[Path] = set() if seen is None else seen
    resolved_header = header.resolve()
    if resolved_header in visited:
        return {}
    visited.add(resolved_header)
    content = resolved_header.read_text(encoding="utf-8")
    macros: dict[str, str] = {}

    for line in content.splitlines():
        trimmed = line.strip()
        if not trimmed.startswith("#"):
            continue
        inc = _INCLUDE_PARSER.search(trimmed)
        if inc:
            include_path = resolved_header.parent / str(inc["header"])
            if include_path.is_file():
                macros.update(_read_variant_macros(include_path, visited))
            continue
        tokens = trimmed.removeprefix("#").strip().split(None, 2)
        if len(tokens) >= 3 and tokens[0] == "define":
            name, val = tokens[1], tokens[2]
            macros[name] = val.split("//", maxsplit=1)[0].strip()
    return macros


def _resolve_macro(name: str, macros: dict[str, str], seen: set[str] | None = None) -> str:
    visited = seen or set()
    if name not in macros or name in visited:
        return macros.get(name, name)
    visited.add(name)
    raw = macros[name]
    token = raw.strip().strip("()").strip().rstrip("uUlL")
    if token in macros:
        return _resolve_macro(token, macros, visited)
    return raw


def _parse_integer(value: str, name: str) -> int:
    token = value.strip().strip("()").strip().rstrip("uUlL")
    try:
        return int(token, 0)
    except ValueError as exc:
        raise ValueError(f"Arduino core metadata {name} is not an integer literal: {value!r}") from exc


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
        led_builtin=_parse_integer(_resolve_macro("LED_BUILTIN", macros), "LED_BUILTIN"),
        digital_pins=_parse_integer(_resolve_macro("NUM_DIGITAL_PINS", macros), "NUM_DIGITAL_PINS"),
        analog_inputs=_parse_integer(_resolve_macro("NUM_ANALOG_INPUTS", macros), "NUM_ANALOG_INPUTS"),
        spi_ss=_parse_integer(_resolve_macro("PIN_SPI_SS", macros), "PIN_SPI_SS"),
        spi_mosi=_parse_integer(_resolve_macro("PIN_SPI_MOSI", macros), "PIN_SPI_MOSI"),
        spi_miso=_parse_integer(_resolve_macro("PIN_SPI_MISO", macros), "PIN_SPI_MISO"),
        spi_sck=_parse_integer(_resolve_macro("PIN_SPI_SCK", macros), "PIN_SPI_SCK"),
        i2c_sda=_parse_integer(_resolve_macro("PIN_WIRE_SDA", macros), "PIN_WIRE_SDA"),
        i2c_scl=_parse_integer(_resolve_macro("PIN_WIRE_SCL", macros), "PIN_WIRE_SCL"),
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
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read Arduino core metadata from {path}: {exc}") from exc
    expected_fields = {field.name for field in fields(ArduinoCoreMetadata)}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid Arduino core metadata shape in {path}")
    metadata = cast(dict[str, object], data)
    if set(metadata) != expected_fields:
        raise ValueError(f"Invalid Arduino core metadata shape in {path}")

    fqbn, mcu = metadata["fqbn"], metadata["mcu"]
    if not isinstance(fqbn, str) or not isinstance(mcu, str):
        raise ValueError(f"Invalid Arduino core metadata values in {path}")
    integer_fields: dict[str, int] = {}
    for name in expected_fields - {"fqbn", "mcu"}:
        value = metadata[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Invalid Arduino core metadata value for {name} in {path}")
        integer_fields[name] = value

    return ArduinoCoreMetadata(
        fqbn=fqbn,
        mcu=mcu,
        frequency_hz=integer_fields["frequency_hz"],
        led_builtin=integer_fields["led_builtin"],
        digital_pins=integer_fields["digital_pins"],
        analog_inputs=integer_fields["analog_inputs"],
        spi_ss=integer_fields["spi_ss"],
        spi_mosi=integer_fields["spi_mosi"],
        spi_miso=integer_fields["spi_miso"],
        spi_sck=integer_fields["spi_sck"],
        i2c_sda=integer_fields["i2c_sda"],
        i2c_scl=integer_fields["i2c_scl"],
    )


app = typer.Typer(help=__doc__, add_completion=False)


@app.command()
def main_cmd(
    fqbn: Annotated[str | None, typer.Option("--fqbn", help="Target Arduino board FQBN")] = None,
    host_test: Annotated[bool, typer.Option("--host-test", help="Use host test FQBN (arduino:avr:uno)")] = False,
    header_path: Annotated[Path | None, typer.Option("--write-header", help="Write C++ header to path")] = None,
    json_path: Annotated[Path | None, typer.Option("--json-output", help="Write JSON metadata to path")] = None,
) -> None:
    if not host_test and not fqbn:
        print(
            "Arduino core metadata error: at least one of --fqbn or --host-test is required",
            file=sys.stderr,
        )
        raise typer.Exit(code=2)
    if host_test and fqbn:
        print("Arduino core metadata error: --fqbn and --host-test are mutually exclusive", file=sys.stderr)
        raise typer.Exit(code=2)
    if header_path is None and json_path is None:
        print(
            "Arduino core metadata error: at least one of --write-header or --json-output is required",
            file=sys.stderr,
        )
        raise typer.Exit(code=2)

    target_fqbn = HOST_TEST_FQBN if host_test else cast(str, fqbn)
    try:
        metadata = resolve_core_metadata(target_fqbn)
        if header_path is not None:
            write_header(metadata, header_path)
        if json_path is not None:
            write_metadata_json(metadata, json_path)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Arduino core metadata error: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from exc


def main(argv: list[str] | None = None) -> int:
    try:
        app(args=argv, standalone_mode=False)
        return 0
    except typer.Exit as exc:
        return exc.exit_code
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0


if __name__ == "__main__":
    app()
