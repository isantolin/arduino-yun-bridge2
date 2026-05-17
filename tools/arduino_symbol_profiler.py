#!/usr/bin/env python3
"""Deep profiling of Arduino ELF files to identify largest symbols (functions/tables)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def resolve_nm_binary() -> str:
    """Resolve AVR nm binary with deterministic fallback order."""
    avr_nm = shutil.which("avr-nm")
    if avr_nm:
        return avr_nm

    arduino_packages = Path.home() / ".arduino15" / "packages"
    if arduino_packages.exists():
        found_nms = sorted(arduino_packages.rglob("avr-nm"))
        if found_nms:
            return str(found_nms[0])

    return shutil.which("nm") or "nm"


def detect_board_label(build_dir: Path, elf_path: Path) -> str:
    """Extract board label from the build path."""
    try:
        rel_parts = elf_path.relative_to(build_dir).parts
    except ValueError:
        rel_parts = elf_path.parts

    for part in rel_parts:
        if part.startswith("arduino-"):
            return part.replace("-", ":", 2)

    if len(rel_parts) > 1:
        return rel_parts[0]
    return "unknown-board"


def profile_elf(build_dir: Path, elf_path: Path, nm_bin: str) -> str:
    """Run nm on the ELF file to extract symbol sizes."""
    board_label = detect_board_label(build_dir, elf_path)
    try:
        cmd = [nm_bin, "--size-sort", "--print-size", "-C", str(elf_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, OSError) as err:
        return f"⚠️ Error profiling {elf_path}: {err}\n"

    lines = [line for line in result.stdout.strip().splitlines() if line]
    lines.reverse()
    top_20 = lines[:20]

    md_lines = [
        f"#### 🔍 Symbol Profiling: {elf_path.name} ({board_label})",
        "",
        "| Size (Bytes) | Type | Symbol Name |",
        "| :--- | :---: | :--- |",
    ]

    for line in top_20:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            size_dec = int(parts[1], 16)
        except ValueError:
            continue
        sym_type = parts[2]
        sym_name = " ".join(parts[3:])
        md_lines.append(f"| {size_dec:,} | `{sym_type}` | `{sym_name}` |")

    return "\n".join(md_lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Profile Arduino ELF symbols.")
    parser.add_argument("build_dir", type=Path, help="Directory containing .elf files.")
    parser.add_argument("--github-step-summary", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None, help="Save report to a file.")

    args = parser.parse_args(argv)
    build_dir: Path = args.build_dir
    github_step_summary: Path | None = args.github_step_summary
    output_file: Path | None = args.output

    if not build_dir.exists():
        print(f"Error: {build_dir} not found.", file=sys.stderr)
        return

    nm_bin = resolve_nm_binary()
    reports: list[str] = []
    seen_paths: set[Path] = set()
    seen_sections: set[tuple[str, str]] = set()

    # Search for .elf files in build directories
    for elf_file in sorted(build_dir.rglob("*.elf")):
        resolved = elf_file.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)

        board_label = detect_board_label(build_dir, elf_file)
        section_key = (board_label, elf_file.name)
        if section_key in seen_sections:
            continue
        seen_sections.add(section_key)

        reports.append(profile_elf(build_dir, elf_file, nm_bin))

    if not reports:
        print("No ELF files found for profiling.", file=sys.stderr)
        return

    full_report = "### 🛠️ C++ Advanced Profiling (Top Symbols)\n\n" + "\n".join(reports)
    print(full_report)

    if github_step_summary:
        with github_step_summary.open("a", encoding="utf-8") as f:
            f.write("\n---\n" + full_report + "\n")

    if output_file:
        output_file.write_text(full_report, encoding="utf-8")


if __name__ == "__main__":
    main()
