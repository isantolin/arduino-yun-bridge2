#!/usr/bin/env python3
"""Deep profiling of Arduino ELF files to identify largest symbols (functions/tables)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def profile_elf(elf_path: Path) -> str:
    """Run nm on the ELF file to extract symbol sizes."""
    try:
        # We try to use avr-nm if available, otherwise fallback to nm
        nm_bin = (
            "avr-nm"
            if subprocess.run(
                ["which", "avr-nm"], capture_output=True, check=False
            ).returncode
            == 0
            else "nm"
        )

        cmd = [nm_bin, "--size-sort", "--print-size", "-C", str(elf_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        lines = result.stdout.strip().split("\n")
        # Reverse to get largest symbols first
        lines.reverse()

        # Take Top 20
        top_20 = lines[:20]

        md_lines = [
            f"#### 🔍 Symbol Profiling: {elf_path.name}",
            "",
            "| Size (Bytes) | Type | Symbol Name |",
            "| :--- | :---: | :--- |",
        ]

        for line in top_20:
            parts = line.split()
            if len(parts) >= 4:
                size_hex = parts[1]
                size_dec = int(size_hex, 16)
                sym_type = parts[2]
                sym_name = " ".join(parts[3:])
                md_lines.append(f"| {size_dec:,} | `{sym_type}` | `{sym_name}` |")

        return "\n".join(md_lines) + "\n"
    except Exception as e:
        return f"⚠️ Error profiling {elf_path.name}: {e}\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Profile Arduino ELF symbols.")
    parser.add_argument("build_dir", type=Path, help="Directory containing .elf files.")
    parser.add_argument("--github-step-summary", type=Path, default=None)

    args = parser.parse_args(argv)
    build_dir: Path = args.build_dir
    github_step_summary: Path | None = args.github_step_summary

    if not build_dir.exists():
        print(f"Error: {build_dir} not found.", file=sys.stderr)
        return

    reports = []
    # Search for .elf files in build directories
    for elf_file in build_dir.rglob("*.elf"):
        reports.append(profile_elf(elf_file))

    if not reports:
        print("No ELF files found for profiling.", file=sys.stderr)
        return

    full_report = "### 🛠️ C++ Advanced Profiling (Top Symbols)\n\n" + "\n".join(reports)
    print(full_report)

    if github_step_summary:
        with github_step_summary.open("a", encoding="utf-8") as f:
            f.write("\n---\n" + full_report + "\n")


if __name__ == "__main__":
    main()
