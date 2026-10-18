#!/usr/bin/env python3
"""Consolidate multi-architecture hardware emulation summaries into a single unified matrix. [SIL-2 / Rule 37]"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    help="Consolidate simavr and Renode hardware emulation summaries into a single matrix.",
    add_completion=False,
)

_MD_ROW_PATTERN = re.compile(
    r"^\|\s*\*\*(?P<name>[^*]+)\*\*(?:<br>`(?P<fqbn>[^`]+)`)?\s*\|\s*(?P<arch>[^|]+)\|\s*(?P<comp>[^|]+)\|\s*(?P<emu>[^|]+)\|\s*\*\*(?P<res>[^*]+)\*\*\s*\|"
)


def _load_engine_boards(
    summary_dir: Path,
    json_filename: str,
    md_filename: str,
    default_engine: str,
    default_arch: str,
) -> list[dict[str, str]]:
    """Load board results from JSON if present, falling back to parsing markdown table rows."""
    json_path = summary_dir / json_filename
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            engine = data.get("engine", default_engine)
            boards: list[dict[str, str]] = [
                {
                    "board_fqbn": b.get("board_fqbn", ""),
                    "board_name": b.get("board_name", ""),
                    "architecture": b.get("architecture", default_arch),
                    "engine": engine,
                    "compilation": b.get("compilation", "⚠️ Unknown"),
                    "emulation": b.get("emulation", "⚠️ Unknown"),
                    "result": b.get("result", "❌ FAIL"),
                }
                for b in data.get("boards", [])
            ]
            return boards
        except (json.JSONDecodeError, OSError):
            pass

    md_path = summary_dir / md_filename
    if md_path.exists():
        try:
            lines = md_path.read_text(encoding="utf-8").splitlines()
            boards = []
            for line in lines:
                m = _MD_ROW_PATTERN.match(line.strip())
                if m:
                    boards.append(
                        {
                            "board_fqbn": (m.group("fqbn") or "").strip(),
                            "board_name": m.group("name").strip(),
                            "architecture": m.group("arch").strip(),
                            "engine": default_engine,
                            "compilation": m.group("comp").strip(),
                            "emulation": m.group("emu").strip(),
                            "result": f"**{m.group('res').strip()}**",
                        }
                    )
            return boards
        except OSError:
            pass

    return []


def generate_consolidated_markdown(
    simavr_dir: Path,
    renode_dir: Path,
) -> str:
    """Build unified multi-architecture hardware emulation table."""
    all_boards: list[dict[str, str]] = []

    simavr_boards = _load_engine_boards(
        simavr_dir,
        "simavr_summary.json",
        "simavr_summary.md",
        default_engine="simavr (Cycle-Accurate)",
        default_arch="AVR 8-bit",
    )
    all_boards.extend(simavr_boards)

    renode_boards = _load_engine_boards(
        renode_dir,
        "renode_summary.json",
        "renode_summary.md",
        default_engine="Renode (Instruction-Accurate)",
        default_arch="ARM Cortex-M0+",
    )
    all_boards.extend(renode_boards)

    header = [
        "### 🔬 Multi-Architecture Hardware Emulation Matrix",
        "",
        "| Board / Target | MCU Architecture | Emulator Engine | Compilation | Emulation (PTY/UART) | Result |",
        "| :--- | :---: | :---: | :---: | :---: | :---: |",
    ]

    if not all_boards:
        header.append("| *(No hardware emulation data found)* | - | - | - | - | ⏭️ SKIPPED |")
        return "\n".join(header) + "\n"

    rows: list[str] = []
    for b in all_boards:
        res = b["result"]
        if not res.startswith("**"):
            res = f"**{res}**"
        fqbn_part = f"<br>`{b['board_fqbn']}`" if b["board_fqbn"] else ""
        rows.append(
            f"| **{b['board_name']}**{fqbn_part} | {b['architecture']} | {b['engine']} | "
            f"{b['compilation']} | {b['emulation']} | {res} |"
        )

    return "\n".join(header + rows) + "\n"


@app.command()
def main(
    simavr_dir: Annotated[
        Path,
        typer.Option("--simavr-dir", help="Directory containing simavr logs and summaries"),
    ] = Path("simavr-logs"),
    renode_dir: Annotated[
        Path,
        typer.Option("--renode-dir", help="Directory containing renode logs and summaries"),
    ] = Path("renode-logs"),
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional output path for consolidated markdown"),
    ] = None,
    step_summary: Annotated[
        bool,
        typer.Option("--step-summary/--no-step-summary", help="Append report to GITHUB_STEP_SUMMARY"),
    ] = True,
) -> None:
    """Consolidate hardware emulation results into single matrix markdown."""
    markdown_content = generate_consolidated_markdown(simavr_dir, renode_dir)
    print(markdown_content)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown_content, encoding="utf-8")

    if step_summary:
        env_step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if env_step_summary:
            try:
                with Path(env_step_summary).open("a", encoding="utf-8") as f:
                    f.write(markdown_content)
            except OSError as exc:
                typer.echo(f"[WARN] Failed to write to GITHUB_STEP_SUMMARY: {exc}", err=True)


if __name__ == "__main__":
    app()
