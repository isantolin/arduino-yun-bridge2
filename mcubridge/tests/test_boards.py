"""Unit tests for tools/boards.py (SIL-2 / Rule 15 / Rule 17)."""

from __future__ import annotations

from typer.testing import CliRunner

from tools.boards import (
    BOARD_MEGA,
    BOARD_MKRWIFI1010,
    BOARD_NANO_ESP32,
    BOARDS_BY_FQBN,
    BOARDS_BY_SLUG,
    FLASH_SECTION_NAMES,
    OFFICIAL_BOARDS,
    RAM_SECTION_NAMES,
    RENODE_BOARDS,
    SIMAVR_BOARDS,
    cli,
    get_board_by_fqbn,
    get_board_by_slug,
    get_official_fqbns,
)

runner = CliRunner()


def test_official_boards_metadata() -> None:
    assert len(OFFICIAL_BOARDS) == 3
    assert BOARD_MEGA in OFFICIAL_BOARDS
    assert BOARD_MKRWIFI1010 in OFFICIAL_BOARDS
    assert BOARD_NANO_ESP32 in OFFICIAL_BOARDS

    assert BOARD_MEGA.fqbn == "arduino:avr:mega"
    assert BOARD_MEGA.slug == "arduino-avr-mega"
    assert BOARD_MEGA.emulator == "simavr"
    assert BOARD_MEGA.max_flash == 253_952
    assert BOARD_MEGA.max_ram == 8_192

    assert BOARD_MKRWIFI1010.fqbn == "arduino:samd:mkrwifi1010"
    assert BOARD_MKRWIFI1010.slug == "arduino-samd-mkrwifi1010"
    assert BOARD_MKRWIFI1010.emulator == "renode"
    assert BOARD_MKRWIFI1010.max_flash == 262_144
    assert BOARD_MKRWIFI1010.max_ram == 32_768

    assert BOARD_NANO_ESP32.fqbn == "arduino:esp32:nano_nora"
    assert BOARD_NANO_ESP32.slug == "arduino-esp32-nano_nora"
    assert BOARD_NANO_ESP32.emulator is None
    assert BOARD_NANO_ESP32.max_flash == 3_145_728
    assert BOARD_NANO_ESP32.max_ram == 327_680


def test_board_lookups() -> None:
    assert get_board_by_slug("arduino-avr-mega") == BOARD_MEGA
    assert get_board_by_slug("arduino-samd-mkrwifi1010") == BOARD_MKRWIFI1010
    assert get_board_by_slug("arduino-esp32-nano_nora") == BOARD_NANO_ESP32
    assert get_board_by_slug("unknown-board") is None

    assert get_board_by_fqbn("arduino:avr:mega") == BOARD_MEGA
    assert get_board_by_fqbn("arduino:samd:mkrwifi1010") == BOARD_MKRWIFI1010
    assert get_board_by_fqbn("arduino:esp32:nano_nora") == BOARD_NANO_ESP32
    assert get_board_by_fqbn("unknown:fqbn") is None

    assert BOARDS_BY_SLUG["arduino-avr-mega"] == BOARD_MEGA
    assert BOARDS_BY_FQBN["arduino:avr:mega"] == BOARD_MEGA
    assert get_official_fqbns() == (
        "arduino:avr:mega",
        "arduino:samd:mkrwifi1010",
        "arduino:esp32:nano_nora",
    )


def test_emulation_matrices() -> None:
    assert SIMAVR_BOARDS == [("arduino:avr:mega", "Arduino Mega 2560 (ATmega2560)")]
    assert RENODE_BOARDS == [("arduino:samd:mkrwifi1010", "Arduino MKR WiFi 1010 (ATSAMD21G18A)")]


def test_section_names() -> None:
    assert ".text" in FLASH_SECTION_NAMES
    assert ".flash.text" in FLASH_SECTION_NAMES
    assert ".iram0.text" in FLASH_SECTION_NAMES
    assert len(FLASH_SECTION_NAMES) == 11

    assert ".bss" in RAM_SECTION_NAMES
    assert ".dram0.bss" in RAM_SECTION_NAMES
    assert len(RAM_SECTION_NAMES) == 5


def test_cli_fqbns() -> None:
    result = runner.invoke(cli, ["fqbns"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "arduino:avr:mega arduino:samd:mkrwifi1010 arduino:esp32:nano_nora"

    custom_delim = runner.invoke(cli, ["fqbns", "--delimiter", ","])
    assert custom_delim.exit_code == 0
    assert custom_delim.stdout.strip() == "arduino:avr:mega,arduino:samd:mkrwifi1010,arduino:esp32:nano_nora"


def test_cli_slugs() -> None:
    result = runner.invoke(cli, ["slugs"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "arduino-avr-mega arduino-samd-mkrwifi1010 arduino-esp32-nano_nora"


def test_cli_list() -> None:
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "Arduino Mega 2560 | FQBN: arduino:avr:mega" in result.stdout
    assert "Arduino MKR WiFi 1010 | FQBN: arduino:samd:mkrwifi1010" in result.stdout
    assert "Arduino Nano ESP32 | FQBN: arduino:esp32:nano_nora" in result.stdout
