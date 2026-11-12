#!/usr/bin/env python3
"""Deep profiling of Arduino ELF files to identify largest symbols using pyelftools."""

from __future__ import annotations
import re
import shutil
import subprocess
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


def read_elf_memory_usage(elf_path: Path) -> tuple[int, int] | None:
    """Read Flash and RAM bytes from ELF sections directly using pyelftools. [SIL-2 / Rule 37]"""
    try:
        with elf_path.open("rb") as f:
            parsed_elf = ELFFile(f)
            sec_sizes = {
                s.name: s["sh_size"]
                for s in parsed_elf.iter_sections()
                if s.name in FLASH_SECTION_NAMES or s.name in RAM_SECTION_NAMES
            }
            flash = sum(sec_sizes.get(name, 0) for name in FLASH_SECTION_NAMES)
            ram = sum(sec_sizes.get(name, 0) for name in RAM_SECTION_NAMES)
            return (flash, ram)
    except (OSError, ValueError, ELFError) as err:
        sys.stderr.write(f"[WARN] Failed to read ELF memory usage from {elf_path}: {err}\n")
        return None


def format_delta(current: int, baseline: int | None) -> str:
    """Format size differential with human-readable indicators."""
    if baseline is None:
        return "N/A"
    delta = current - baseline
    if delta > 0:
        return f"+{delta:,} B 🔺"
    if delta < 0:
        return f"{delta:,} B 🟢"
    return "0 B ➖"


def find_matching_baseline_elf(current_elf: Path, current_build_dir: Path, baseline_path: Path) -> Path | None:
    """Locate matching baseline ELF binary for differential comparison."""
    if baseline_path.is_file():
        return baseline_path
    if not baseline_path.is_dir():
        return None
    try:
        rel = current_elf.relative_to(current_build_dir)
        candidate = baseline_path / rel
        if candidate.is_file():
            return candidate
    except ValueError as err:
        sys.stderr.write(f"[DEBUG] Path {current_elf} is not relative to {current_build_dir}: {err}\n")
    matches = sorted(baseline_path.rglob(current_elf.name))
    return matches[0] if matches else None


def run_pw_bloat(elf_path: Path, baseline_elf: Path | None = None) -> str | None:
    """Execute Pigweed pw_bloat on target ELF binary if available. [SIL-2]"""
    if not shutil.which("bloaty"):
        return None

    config_path = Path(__file__).parent / "bloaty_mcu.config"
    cmd = [sys.executable, "-m", "pw_bloat", str(elf_path)]
    if config_path.is_file():
        cmd.extend(["-c", str(config_path)])
    if baseline_elf and baseline_elf.is_file():
        cmd.extend(["--diff", str(baseline_elf)])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError) as err:
        sys.stderr.write(f"[WARN] Failed to run pw_bloat on {elf_path}: {err}\n")
    return None


def extract_elf_memory_table(build_dir: Path, baseline_dir: Path | None = None) -> str | None:
    """Extract Flash and RAM usage and delta directly from ELF sections using pyelftools. [SIL-2]"""
    if not build_dir.exists() or not build_dir.is_dir():
        return None
    elf_files = sorted(build_dir.rglob("*.elf"))
    if not elf_files:
        return None

    rows: list[str] = []
    has_diff = baseline_dir is not None

    for elf in elf_files:
        mem = read_elf_memory_usage(elf)
        if mem is None:
            continue
        flash_used, ram_used = mem
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

        if baseline_dir is not None:
            base_elf = find_matching_baseline_elf(elf, build_dir, baseline_dir)
            base_mem = read_elf_memory_usage(base_elf) if base_elf else None
            base_flash = base_mem[0] if base_mem else None
            base_ram = base_mem[1] if base_mem else None
            delta_flash = format_delta(flash_used, base_flash)
            delta_ram = format_delta(ram_used, base_ram)
            rows.append(
                f"| {label} | `{sketch}` | {flash_used:,} / {max_flash_str} ({flash_pct}) | {delta_flash} | "
                f"{ram_used:,} / {max_ram_str} ({ram_pct}) | {delta_ram} |"
            )
        else:
            rows.append(
                f"| {label} | `{sketch}` | {flash_used:,} / {max_flash_str} | {flash_pct} | "
                f"{ram_used:,} / {max_ram_str} | {ram_pct} |"
            )

    if not rows:
        return None

    if has_diff:
        header = [
            "### 📊 Arduino Memory Usage & Differential",
            "",
            "| Board | Sketch | Flash (Used / Max) | Δ Flash | RAM (Used / Max) | Δ RAM |",
            "| :--- | :--- | :---: | :---: | :---: | :---: |",
        ]
    else:
        header = [
            "### 📊 Arduino Memory Usage",
            "",
            "| Board | Sketch | Flash (Used / Max) | Flash % | RAM (Used / Max) | RAM % |",
            "| :--- | :--- | :---: | :---: | :---: | :---: |",
        ]
    return "\n".join(header + rows) + "\n\n"


def extract_stack_usage_report(build_dir: Path, limit: int = 20) -> str | None:
    """Report largest GCC per-function stack frames from -fstack-usage files.

    These are individual frame estimates, not a whole-program worst-case call
    chain. Dynamic/unbounded entries are identified rather than treated as
    static upper bounds.
    """
    entries: list[tuple[int, str, str, str]] = []
    for usage_file in sorted(build_dir.rglob("*.su")):
        try:
            lines = usage_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as err:
            sys.stderr.write(f"[WARN] Failed to read stack usage file {usage_file}: {err}\n")
            continue
        for line in lines:
            fields = line.rsplit("\t", 2)
            if len(fields) != 3:
                continue
            location_and_function, size_text, qualifier = fields
            try:
                size = int(size_text)
            except ValueError:
                continue
            location_match = re.match(
                r"^(.*):(\d+):(\d+):(.*)$", location_and_function
            )
            if location_match:
                source = (
                    f"{location_match.group(1)}:"
                    f"{location_match.group(2)}:{location_match.group(3)}"
                )
                function = location_match.group(4)
            else:
                source, function = str(usage_file), location_and_function
            entries.append((size, function, source, qualifier))

    if not entries:
        return None

    entries.sort(key=lambda item: item[0], reverse=True)
    rows = [
        "### Compiler Stack-Frame Estimates (GCC -fstack-usage)",
        "",
        (
            "Largest individual function frames. These are not a whole-program worst-case "
            "call-chain bound; dynamic frames and indirect calls require separate analysis."
        ),
        "",
        "| Stack frame | GCC classification | Function | Source |",
        "| ---: | :--- | :--- | :--- |",
    ]
    for size, function, source, qualifier in entries[:limit]:
        rows.append(f"| {size:,} B | `{qualifier}` | `{function}` | `{source}` |")
    rows.append("")
    return "\n".join(rows) + "\n"

def detect_board_label(build_dir: Path, elf_path: Path) -> str:
    """Extract board label from the build path."""
    for part in reversed(elf_path.parts):
        spec = SUPPORTED_BOARDS.get(part)
        if spec is not None:
            return spec.label
        if part.startswith("arduino-"):
            return part.replace("-", ":", 2)

    try:
        parts = elf_path.relative_to(build_dir).parts
        return parts[0] if len(parts) > 1 else "unknown-board"
    except ValueError:
        return elf_path.parts[0] if elf_path.parts else "unknown-board"


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
    diff: Annotated[
        Path | None,
        typer.Option("--diff", "-d", help="Baseline build directory or ELF file for differential comparison"),
    ] = None,
) -> None:
    """Generate symbol profiling report and Pigweed size analysis for all ELF files in build directory."""
    if not build_dir.exists():
        sys.stderr.write(f"Error: {build_dir} not found.\n")
        return

    elf_files = sorted(build_dir.rglob("*.elf"))
    reports = [profile_elf(build_dir, elf) for elf in elf_files]
    mem_report = extract_elf_memory_table(build_dir, baseline_dir=diff) or ""

    bloat_reports: list[str] = []
    for elf in elf_files:
        base_elf = find_matching_baseline_elf(elf, build_dir, diff) if diff else None
        bloat_out = run_pw_bloat(elf, base_elf)
        if bloat_out:
            board = detect_board_label(build_dir, elf)
            bloat_reports.append(f"#### Board: {board} (`{elf.name}`)\n\n```text\n{bloat_out}\n```\n")

    bloat_section = (
        ("### 🔬 Pigweed Size Report (pw_bloat)\n\n" + "\n".join(bloat_reports) + "\n") if bloat_reports else ""
    )

    if not reports and not mem_report and not bloat_reports:
        sys.stderr.write("No ELF files found for profiling.\n")
        return

    symbol_section = ("### 🛠️ C++ Advanced Profiling (Top Symbols)\n\n" + "\n".join(reports)) if reports else ""
    stack_report = extract_stack_usage_report(build_dir) or ""
    full_report = mem_report + stack_report + bloat_section + symbol_section

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(full_report, encoding="utf-8")
        print(f"✅ Symbol profile saved to {output}")
    else:
        print(full_report)


if __name__ == "__main__":
    cli()
