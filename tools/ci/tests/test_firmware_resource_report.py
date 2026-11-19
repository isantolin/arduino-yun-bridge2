from __future__ import annotations

import unittest

from tools.ci.firmware_resource_report import (
    parse_avr_sections,
    parse_build_summary,
)


class FirmwareResourceReportTest(unittest.TestCase):
    def test_parses_arduino_cli_summary_with_thousands_separators(self) -> None:
        report = parse_build_summary(
            "Sketch uses 123,456 bytes (48%) of program storage space. "
            "Maximum is 253,952 bytes.\n"
            "Global variables use 2,048 bytes (25%) of dynamic memory, "
            "leaving 6,144 bytes for local variables. Maximum is 8,192 bytes.\n"
        )
        self.assertEqual(report["flash_used_bytes"], 123456)
        self.assertEqual(report["flash_limit_bytes"], 253952)
        self.assertEqual(report["static_ram_used_bytes"], 2048)
        self.assertEqual(report["static_ram_remaining_bytes"], 6144)
        self.assertEqual(report["static_ram_limit_bytes"], 8192)

    def test_rejects_incomplete_cli_summary(self) -> None:
        with self.assertRaisesRegex(ValueError, "resource summary is incomplete"):
            parse_build_summary("Sketch uses 123 bytes.\n")

    def test_calculates_avr_flash_and_static_ram_sections(self) -> None:
        report = parse_avr_sections(
            ".text 10000 0\n.data 200 8388864\n"
            ".bss 700 8389064\n.noinit 16 8389764\n"
        )
        self.assertEqual(report["elf_flash_used_bytes"], 10200)
        self.assertEqual(report["elf_static_ram_used_bytes"], 916)
        self.assertEqual(report["elf_text_bytes"], 10000)

    def test_rejects_incomplete_avr_sections(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing one of"):
            parse_avr_sections(".text 100 0\n.data 10 100\n")


if __name__ == "__main__":
    unittest.main()
