#!/usr/bin/env python3
"""Emit machine-readable firmware resource measurements from Arduino builds.

The Arduino CLI summary is authoritative for each core's reported Flash and
static-RAM use. AVR ELF sections are additionally measured with avr-size when
available. Runtime stack high-water is deliberately not inferred from static
compiler output; simavr's low-water instrumentation is a separate measurement.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_SKETCH_RE = re.compile(
    r"Sketch uses\s+([\d,]+)\s+bytes.*?Maximum is\s+([\d,]+)\s+bytes",
    re.IGNORECASE,
)
_RAM_RE = re.compile(
    r"Global variables use\s+([\d,]+)\s+bytes.*?leaving\s+([\d,]+)\s+bytes.*?Maximum is\s+([\d,]+)\s+bytes",
    re.IGNORECASE,
)
_AVR_SECTION_RE = re.compile(r"^\.(text|data|bss|noinit)\s+(\d+)\s+", re.MULTILINE)


def _integer(value: str) -> int:
    return int(value.replace(",", ""))


def parse_build_summary(log: str) -> dict[str, int]:
    """Parse the stable Arduino CLI Flash and global-RAM summary."""
    flash_matches = list(_SKETCH_RE.finditer(log))
    ram_matches = list(_RAM_RE.finditer(log))
    if not flash_matches or not ram_matches:
        raise ValueError(
            "Arduino CLI resource summary is incomplete; expected both "
            "'Sketch uses ... Maximum is ...' and 'Global variables use ...' lines"
        )
    flash = flash_matches[-1]
    ram = ram_matches[-1]
    return {
        "flash_used_bytes": _integer(flash.group(1)),
        "flash_limit_bytes": _integer(flash.group(2)),
        "static_ram_used_bytes": _integer(ram.group(1)),
        "static_ram_remaining_bytes": _integer(ram.group(2)),
        "static_ram_limit_bytes": _integer(ram.group(3)),
    }


def parse_avr_sections(output: str) -> dict[str, int]:
    """Calculate AVR Flash and static SRAM from avr-size -A section output."""
    sections = {name: int(size) for name, size in _AVR_SECTION_RE.findall(output)}
    if not {"text", "data", "bss"}.issubset(sections):
        raise ValueError("avr-size output is missing one of .text, .data, or .bss")
    return {
        "elf_flash_used_bytes": sections["text"] + sections["data"],
        "elf_static_ram_used_bytes": (sections["data"] + sections["bss"] + sections.get("noinit", 0)),
        "elf_text_bytes": sections["text"],
        "elf_data_bytes": sections["data"],
        "elf_bss_bytes": sections["bss"],
        "elf_noinit_bytes": sections.get("noinit", 0),
    }


def measure(
    compile_log: str,
    fqbn: str,
    elf_path: Path | None = None,
    size_tool: str | None = None,
) -> dict[str, Any]:
    """Build a resource report without estimating runtime stack usage."""
    report: dict[str, Any] = {
        "schema_version": 1,
        "target": fqbn,
        "measurement_source": "arduino-cli compile summary",
        **parse_build_summary(compile_log),
        "runtime_stack_peak_bytes": None,
        "runtime_stack_measurement": (
            "not measured by this static build report; target-specific "
            "runtime stack-watermark instrumentation is required"
        ),
    }

    if elf_path is not None and size_tool:
        result = subprocess.run(
            [size_tool, "-A", str(elf_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        report.update(parse_avr_sections(result.stdout))
        report["measurement_source"] += " + avr-size -A"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", required=True, type=Path)
    parser.add_argument("--fqbn", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--elf", type=Path)
    parser.add_argument("--size-tool")
    args = parser.parse_args()

    report = measure(
        args.compile_log.read_text(encoding="utf-8"),
        args.fqbn,
        args.elf,
        args.size_tool,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
