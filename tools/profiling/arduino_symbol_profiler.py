#!/usr/bin/env python3
"""Deep profiling of Arduino ELF files to identify largest symbols using pyelftools."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

import typer

from tools.boards import (
    BOARDS_BY_SLUG as SUPPORTED_BOARDS,
    FLASH_SECTION_NAMES,
    RAM_SECTION_NAMES,
)


def extract_elf_memory_table(build_dir: Path) -> str | None:
    """Extract Flash and RAM usage table directly from ELF sections using pyelftools. [SIL-2 / Rule 37]"""
    if not build_dir.exists() or not build_dir.is_dir():
        return None
    elf_files = sorted(build_dir.rglob("*.elf"))
    if not elf_files:
        return None

    rows: list[str] = []
    for elf in elf_files:
        try:
            with elf.open("rb") as f:
                parsed_elf = ELFFile(f)
                sec_sizes = {
                    s.name: s["sh_size"]
                    for s in parsed_elf.iter_sections()
                    if s.name in FLASH_SECTION_NAMES or s.name in RAM_SECTION_NAMES
                }
                flash_used = sum(sec_sizes.get(name, 0) for name in FLASH_SECTION_NAMES)
                ram_used = sum(sec_sizes.get(name, 0) for name in RAM_SECTION_NAMES)
                if flash_used == 0 and ram_used == 0:
                    continue

                board_key = ""
                for part in reversed(elf.parts):
                    if part in SUPPORTED_BOARDS:
                        board_key = part
                        break

                spec = SUPPORTED_BOARDS.get(board_key)
                label = spec.label if spec is not None else detect_board_label(build_dir, elf)
                max_flash = spec.max_flash if spec is not None else 0
                max_ram = spec.max_ram if spec is not None else 0
                sketch = elf.stem.replace(".ino", "")
                flash_pct = f"{(flash_used / max_flash * 100):.0f}%" if max_flash > 0 else "N/A"
                ram_pct = f"{(ram_used / max_ram * 100):.0f}%" if max_ram > 0 else "N/A"
                max_flash_str = f"{max_flash:,} B" if max_flash > 0 else "N/A"
                max_ram_str = f"{max_ram:,} B" if max_ram > 0 else "N/A"
                rows.append(
                    f"| {label} | `{sketch}` | {flash_used:,} / {max_flash_str} | {flash_pct} | "
                    f"{ram_used:,} / {max_ram_str} | {ram_pct} |"
                )
        except (OSError, ValueError, ELFError) as err:
            sys.stderr.write(f"[WARN] Failed to read ELF sections with pyelftools {elf}: {err}\n")
            continue

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
        spec = SUPPORTED_BOARDS.get(part)
        if spec is not None:
            return spec.label
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
    mem_report = extract_elf_memory_table(build_dir) or ""

    if not reports and not mem_report:
        sys.stderr.write("No ELF files found for profiling.\n")
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
