from __future__ import annotations

from pathlib import Path

import pytest

from tools.arduino_core_metadata import (
    ArduinoCoreMetadata,
    metadata_from_properties,
    metadata_header,
    read_metadata_json,
    write_header,
    write_metadata_json,
)

UNO_METADATA = ArduinoCoreMetadata(
    fqbn="arduino:avr:uno",
    mcu="atmega328p",
    frequency_hz=16_000_000,
    led_builtin=13,
    digital_pins=20,
    analog_inputs=6,
    spi_ss=10,
    spi_mosi=11,
    spi_miso=12,
    spi_sck=13,
    i2c_sda=18,
    i2c_scl=19,
)


def _core_files(tmp_path: Path, include_sck: bool = True) -> Path:
    variant = tmp_path / "variants" / "yun"
    inherited_variant = tmp_path / "variants" / "leonardo"
    variant.mkdir(parents=True)
    inherited_variant.mkdir(parents=True)
    sck_macro = "#define PIN_SPI_SCK 15\n" if include_sck else ""
    (inherited_variant / "pins_arduino.h").write_text(
        "#define NUM_DIGITAL_PINS 31\n"
        "#define NUM_ANALOG_INPUTS 12\n"
        "#define LED_BUILTIN 13\n"
        "#define PIN_SPI_SS 17\n"
        "#define PIN_SPI_MOSI 16\n"
        "#define PIN_SPI_MISO 14\n"
        f"{sck_macro}"
        "#define PIN_WIRE_SDA 2\n"
        "#define PIN_WIRE_SCL 3\n",
        encoding="utf-8",
    )
    (variant / "pins_arduino.h").write_text(
        '#include "../leonardo/pins_arduino.h"\n#define LED_BUILTIN 7\n',
        encoding="utf-8",
    )
    properties = tmp_path / "yun.properties"
    properties.write_text(
        f"build.f_cpu=16000000L\nbuild.mcu=atmega32u4\nbuild.variant.path={variant}\n",
        encoding="utf-8",
    )
    return properties


def test_metadata_resolves_inherited_variant_macros(tmp_path: Path) -> None:
    properties = _core_files(tmp_path)

    metadata = metadata_from_properties(
        "arduino:avr:yun",
        properties.read_text(encoding="utf-8"),
    )

    assert metadata == ArduinoCoreMetadata(
        fqbn="arduino:avr:yun",
        mcu="atmega32u4",
        frequency_hz=16_000_000,
        led_builtin=7,
        digital_pins=31,
        analog_inputs=12,
        spi_ss=17,
        spi_mosi=16,
        spi_miso=14,
        spi_sck=15,
        i2c_sda=2,
        i2c_scl=3,
    )


def test_metadata_rejects_missing_core_macro(tmp_path: Path) -> None:
    properties = _core_files(tmp_path, include_sck=False)

    with pytest.raises(ValueError, match="PIN_SPI_SCK"):
        metadata_from_properties(
            "arduino:avr:yun",
            properties.read_text(encoding="utf-8"),
        )


def test_header_is_generated_from_core_metadata(tmp_path: Path) -> None:
    header = tmp_path / "ArduinoCoreMetadata.h"
    expected = metadata_header(UNO_METADATA)

    write_header(UNO_METADATA, header)
    assert header.read_text(encoding="utf-8") == expected


def test_metadata_json_is_read_from_compiler_output(tmp_path: Path) -> None:
    metadata_file = tmp_path / "metadata.json"
    write_metadata_json(UNO_METADATA, metadata_file)

    assert read_metadata_json(metadata_file) == UNO_METADATA
