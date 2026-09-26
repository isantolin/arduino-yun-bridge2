#!/usr/bin/env python3
"""Deep profiling of Arduino ELF files to identify largest symbols using Bloaty."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer


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
    build_dir: Annotated[Path, typer.Argument(help="Directory containing .elf files.")],
    github_step_summary: Annotated[
        Path | None,
        typer.Option("--github-step-summary", help="Path to GitHub step summary markdown output"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Save report to a file."),
    ] = None,
) -> None:
    if not build_dir.exists():
        sys.stderr.write(f"Error: {build_dir} not found.\n")
        return

    bloaty_bin = shutil.which("bloaty")
    reports = [profile_elf(build_dir, elf, bloaty_bin) for elf in sorted(build_dir.rglob("*.elf"))]
    if not reports:
        sys.stderr.write("No ELF files found for profiling.\n")
        return

    full_report = "### 🛠️ C++ Advanced Profiling (Top Symbols)\n\n" + "\n".join(reports)
    print(full_report)

    if github_step_summary:
        with github_step_summary.open("a", encoding="utf-8") as f:
            f.write("\n---\n" + full_report + "\n")

    if output:
        output.write_text(full_report, encoding="utf-8")


if __name__ == "__main__":
    cli()
