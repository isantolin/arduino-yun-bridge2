#!/usr/bin/env python3
"""Parse Arduino compilation logs and generate a memory usage report."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Annotated

import typer

app = typer.Typer(help="Parse Arduino compilation logs and generate a memory usage report.")

BOARD_MAPPING = {
    "arduino-avr-yun": "Arduino Yún",
    "arduino-avr-uno": "Arduino Uno",
    "arduino-avr-mega": "Arduino Mega",
}

# Regex patterns for memory usage
# Sketch uses 34982 bytes (13%) of program storage space. Maximum is 253952 bytes.
FLASH_RE = re.compile(r"Sketch uses (\d+) bytes .*? Maximum is (\d+) bytes")
# Global variables use 3628 bytes (44%) of dynamic memory, leaving 4564 bytes for local variables. Maximum is 8192 bytes.
RAM_RE = re.compile(r"Global variables use (\d+) bytes .*? Maximum is (\d+) bytes")


@dataclass
class MemoryMetrics:
    board: str
    sketch: str
    flash_used: int
    flash_max: int
    ram_used: int
    ram_max: int

    @property
    def flash_percent(self) -> float:
        return (self.flash_used / self.flash_max) * 100 if self.flash_max > 0 else 0

    @property
    def ram_percent(self) -> float:
        return (self.ram_used / self.ram_max) * 100 if self.ram_max > 0 else 0


def parse_log_file(path: Path) -> MemoryMetrics | None:
    """Extract metrics from a single log file."""
    parts = path.stem.split("_", 1)
    if len(parts) < 2:
        return None

    board_slug, sketch = parts
    board_name = BOARD_MAPPING.get(board_slug, board_slug)

    content = path.read_text(encoding="utf-8")
    
    flash_match = FLASH_RE.search(content)
    ram_match = RAM_RE.search(content)

    if not flash_match or not ram_match:
        return None

    return MemoryMetrics(
        board=board_name,
        sketch=sketch,
        flash_used=int(flash_match.group(1)),
        flash_max=int(flash_match.group(2)),
        ram_used=int(ram_match.group(1)),
        ram_max=int(ram_match.group(2)),
    )


def render_markdown(metrics: list[MemoryMetrics]) -> str:
    """Generate Markdown table for the report."""
    header = [
        "### 📊 Arduino Memory Usage",
        "",
        "| Board | Sketch | Flash (Used/Max) | Flash % | RAM (Used/Max) | RAM % |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    
    sorted_metrics = sorted(metrics, key=lambda m: (m.board, m.sketch))
    
    rows = []
    for m in sorted_metrics:
        row = (
            f"| {m.board} | `{m.sketch}` | "
            f"{m.flash_used:,} / {m.flash_max:,} B | {m.flash_percent:.1f}% | "
            f"{m.ram_used:,} / {m.ram_max:,} B | {m.ram_percent:.1f}% |"
        )
        rows.append(row)
    
    return "\n".join(header + rows)


@app.command()
def main(
    log_dir: Annotated[Path, typer.Argument(help="Directory containing build log files.")],
    github_step_summary: Annotated[
        Optional[Path], 
        typer.Option(help="Append the table to GitHub step summary output.")
    ] = None,
) -> None:
    if not log_dir.exists() or not log_dir.is_dir():
        typer.secho(f"Warning: Directory {log_dir} does not exist. Skipping memory report.", fg=typer.colors.YELLOW)
        return

    all_metrics = []
    for log_file in log_dir.glob("*.log"):
        m = parse_log_file(log_file)
        if m:
            all_metrics.append(m)

    if not all_metrics:
        typer.secho("No memory metrics found in logs.", fg=typer.colors.YELLOW)
        return

    md_report = render_markdown(all_metrics)
    print(md_report)

    if github_step_summary:
        with github_step_summary.open("a", encoding="utf-8") as f:
            f.write("\n" + md_report + "\n")


if __name__ == "__main__":
    app()
