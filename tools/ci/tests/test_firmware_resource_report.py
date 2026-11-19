from __future__ import annotations

import pytest

from tools.ci.firmware_resource_report import parse_avr_sections, parse_build_summary


def test_parses_arduino_cli_summary_with_thousands_separators() -> None:
    report = parse_build_summary(
        "Sketch uses 123,456 bytes (48%) of program storage space. "
        "Maximum is 253,952 bytes.\n"
        "Global variables use 2,048 bytes (25%) of dynamic memory, "
        "leaving 6,144 bytes for local variables. Maximum is 8,192 bytes.\n"
    )
    assert report["flash_used_bytes"] == 123456
    assert report["flash_limit_bytes"] == 253952
    assert report["static_ram_used_bytes"] == 2048
    assert report["static_ram_remaining_bytes"] == 6144
    assert report["static_ram_limit_bytes"] == 8192


def test_rejects_incomplete_cli_summary() -> None:
    with pytest.raises(ValueError, match="resource summary is incomplete"):
        parse_build_summary("Sketch uses 123 bytes.\n")


def test_calculates_avr_flash_and_static_ram_sections() -> None:
    report = parse_avr_sections(
        ".text 10000 0\n.data 200 8388864\n"
        ".bss 700 8389064\n.noinit 16 8389764\n"
    )
    assert report["elf_flash_used_bytes"] == 10200
    assert report["elf_static_ram_used_bytes"] == 916
    assert report["elf_text_bytes"] == 10000


def test_rejects_incomplete_avr_sections() -> None:
    with pytest.raises(ValueError, match="missing one of"):
        parse_avr_sections(".text 100 0\n.data 10 100\n")
