#!/usr/bin/env python3
"""Deep profiling of Arduino ELF files to identify largest symbols using Bloaty."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer


def parse_memory_logs(log_dir: Path) -> str | None:
    """Parse Arduino compilation logs and extract Flash and RAM usage table."""
    if not log_dir.exists() or not log_dir.is_dir():
        return None
    logs = sorted(log_dir.glob("*.log"))
    if not logs:
        return None
    mapping = {
        "arduino-avr-yun": "Arduino Yún",
        "arduino-avr-uno": "Arduino Uno",
        "arduino-avr-mega": "Arduino Mega",
    }
    rows: list[str] = []
    for p in logs:
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError as err:
            sys.stderr.write(f"[WARN] Failed to read log file {p}: {err}\n")
            continue
        fm = re.search(r"Sketch uses (\d+) bytes \(([^)]+)\).*Maximum is (\d+) bytes", txt)
        rm = re.search(r"Global variables use (\d+) bytes \(([^)]+)\).*Maximum is (\d+) bytes", txt)
        if fm and rm:
            parts = p.stem.split("_", 1)
            bname = mapping.get(parts[0], parts[0])
            sketch = parts[1] if len(parts) > 1 else parts[0]
            rows.append(
                f"| {bname} | `{sketch}` | {int(fm.group(1)):,} / {int(fm.group(3)):,} B | {fm.group(2)} | "
                f"{int(rm.group(1)):,} / {int(rm.group(3)):,} B | {rm.group(2)} |"
            )
    if not rows:
        return None
    header = [
        "### 📊 Arduino Memory Usage",
        "",
        "| Board | Sketch | Flash (Used / Max) | Flash % | RAM (Used / Max) | RAM % |",
        "| :--- | :--- | :---: | :---: | :---: | :---: |",
    ]
    return "\n".join(header + rows) + "\n\n"


def detect_board_label(build_dir: Path, elf_path: Path) -> str:
    """Extract board label from the build path."""
    try:
        parts = elf_path.relative_to(build_dir).parts
    except ValueError:
        parts = elf_path.parts

    for part in parts:
        if part.startswith("arduino-"):
            return part.replace("-", ":", 2)

    return parts[0] if len(parts) > 1 else "unknown-board"


def profile_elf(build_dir: Path, elf_path: Path, bloaty_bin: str | None = None) -> str:
    """Run Bloaty (or nm fallback) on the ELF file to extract symbol sizes."""
    board = detect_board_label(build_dir, elf_path)
    if bloaty_bin:
        try:
            cmd = [bloaty_bin, "-d", "symbols", "-n", "20", str(elf_path)]
            out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
            return f"#### 🔍 Bloaty Symbol Profiling: {elf_path.name} ({board})\n\n```\n{out}\n```\n"
        except (subprocess.CalledProcessError, OSError) as err:
            sys.stderr.write(f"[WARN] Bloaty error profiling {elf_path}: {err}\n")

    nm_bin = shutil.which("avr-nm") or shutil.which("nm") or "nm"
    try:
        cmd = [nm_bin, "--size-sort", "--print-size", "-C", str(elf_path)]
        lines = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip().splitlines()
        top_20 = lines[-20:][::-1]
        formatted = "\n".join(f"- `{line.strip()}`" for line in top_20)
        return f"#### 🔍 Symbol Profiling: {elf_path.name} ({board})\n\n{formatted}\n"
    except (subprocess.CalledProcessError, OSError) as err:
        return f"⚠️ Error profiling {elf_path}: {err}\n"


cli = typer.Typer(help="Profile Arduino ELF symbols.", add_completion=False)


@cli.command()
def main(
    build_dir: Annotated[Path, typer.Argument(help="Build output directory")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write profile to markdown file"),
    ] = None,
) -> None:
    """Generate symbol profiling report for all ELF files in build directory."""
    if not build_dir.exists():
        sys.stderr.write(f"Error: {build_dir} not found.\n")
        return

    bloaty_bin = shutil.which("bloaty")
    reports = [profile_elf(build_dir, elf, bloaty_bin) for elf in sorted(build_dir.rglob("*.elf"))]
    mem_report = parse_memory_logs(Path("arduino-logs")) or ""

    if not reports and not mem_report:
        sys.stderr.write("No ELF files or memory logs found for profiling.\n")
        return

    symbol_section = ("### 🛠️ C++ Advanced Profiling (Top Symbols)\n\n" + "\n".join(reports)) if reports else ""
    full_report = mem_report + symbol_section

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(full_report, encoding="utf-8")
        print(f"✅ Symbol profile saved to {output}")
    else:
        print(full_report)


if __name__ == "__main__":
    cli()
