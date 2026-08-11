#!/usr/bin/env python3
"""Parse Arduino coverage.json and report coverage metrics."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import typer

app = typer.Typer(help="Parse Arduino coverage.json and report summary.", add_completion=False)


@app.command()
def report(
    coverage_path: Path = Path("coverage/arduino/coverage.json"),
) -> None:
    """Parse coverage metrics and display uncovered lines."""
    if not coverage_path.exists():
        sys.stderr.write(f"Warning: Coverage report '{coverage_path}' not found.\n")
        return

    data = json.loads(coverage_path.read_text(encoding="utf-8"))

    for file_entry in data.get("files", []):
        filename = file_entry.get("file", "")
        # Exclude external dependencies and generated protocol stubs
        if any(ignored in filename for ignored in ("etl", "wolfssl", "wolfcrypt", "rpc_protocol.h", "rpc_structs.h")):
            continue

        lines = file_entry.get("lines", [])
        line_counts: dict[int, int] = {}
        for line in lines:
            ln = line["line_number"]
            line_counts[ln] = line_counts.get(ln, 0) + line["count"]

        f_total = len(line_counts)
        f_covered = sum(1 for count in line_counts.values() if count > 0)
        percent = (f_covered / f_total * 100) if f_total > 0 else 0.0

        uncovered = sorted([ln for ln, count in line_counts.items() if count == 0])
        print(f"{filename}: {percent:.1f}% ({f_covered}/{f_total})")
        if uncovered:
            print(f"  Uncovered: {uncovered[:20]}...")


if __name__ == "__main__":
    app()
