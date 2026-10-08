#!/usr/bin/env python3
"""Deep profiling of Arduino ELF files to identify largest symbols using pyelftools."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Annotated

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

import typer


BOARD_LABELS = {
    "arduino-avr-mega": "Arduino Mega 2560",
    "arduino-samd-mkrwifi1010": "Arduino MKR WiFi 1010",
    "arduino-esp32-nano_nora": "Arduino Nano ESP32",
}


def parse_memory_logs(log_dir: Path) -> str | None:
    """Parse Arduino compilation logs and extract Flash and RAM usage table."""
    if not log_dir.exists() or not log_dir.is_dir():
        return None
    logs = sorted(log_dir.glob("*.log"))
    if not logs:
        return None
    rows: list[str] = []
    for p in logs:
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError as err:
            sys.stderr.write(f"[WARN] Failed to read log file {p}: {err}\n")
            continue
        fm = re.search(
            r"(?:Sketch uses|El Sketch usa) (\d+) bytes \(([^)]+)\).*?(?:Maximum is|El máximo es) (\d+) bytes",
            txt,
            re.IGNORECASE,
        )
        rm = re.search(
            r"(?:Global variables use|Las variables Globales usan) "
            r"(\d+) bytes \(([^)]+)\).*?(?:Maximum is|El máximo es) (\d+) bytes",
            txt,
            re.IGNORECASE,
        )
        if fm and rm:
            parts = p.stem.split("__", 1)
            bname = BOARD_LABELS.get(parts[0], parts[0])
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
        if part in BOARD_LABELS:
            return BOARD_LABELS[part]
        if part.startswith("arduino-"):
            return part.replace("-", ":", 2)

    return parts[0] if len(parts) > 1 else "unknown-board"


def extract_symbols(elf_path: Path, limit: int = 20) -> list[str]:
    """Extract top symbols by size directly using pure-Python pyelftools. [SIL-2 / Rule 37]"""
    syms: list[tuple[int, str, str, str]] = []
    try:
        with elf_path.open("rb") as f:
            elf = ELFFile(f)
            for section in elf.iter_sections():
                if isinstance(section, SymbolTableSection):
                    for sym in section.iter_symbols():
                        size = int(sym["st_size"])
                        name = sym.name or ""
                        if size > 0 and name:
                            st_type = str(sym["st_info"]["type"]).removeprefix("STT_")
                            sec_idx = sym["st_shndx"]
                            sec_name = "UNK"
                            if isinstance(sec_idx, int) and sec_idx < elf.num_sections():
                                sec_name = elf.get_section(sec_idx).name
                            syms.append((size, name, st_type, sec_name))
    except (OSError, ValueError, ELFError) as err:
        sys.stderr.write(f"[WARN] Failed to read ELF with pyelftools {elf_path}: {err}\n")
        return []

    syms.sort(key=lambda s: s[0], reverse=True)
    return [f"- `{size:5d} B` **[{st_type} / {sec}]** `{name}`" for size, name, st_type, sec in syms[:limit]]


def profile_elf(build_dir: Path, elf_path: Path) -> str:
    """Extract symbol sizes using pure-Python pyelftools directly. [SIL-2 / Rule 37]"""
    board = detect_board_label(build_dir, elf_path)
    symbols = extract_symbols(elf_path)
    if not symbols:
        return (
            f"#### 🔍 Symbol Profiling (pyelftools): {elf_path.name} ({board})\n\n"
            "_No symbols found or file unreadable._\n"
        )
    formatted = "\n".join(symbols)
    return f"#### 🔍 Symbol Profiling (pyelftools): {elf_path.name} ({board})\n\n{formatted}\n"


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

    reports = [profile_elf(build_dir, elf) for elf in sorted(build_dir.rglob("*.elf"))]
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
