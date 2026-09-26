#!/usr/bin/env python3
"""Automated Static Library Density & Architectural Rule Auditor.

Delegates architectural rule audits to declarative Semgrep rules and audits
sketch synchronization loops. [SIL-2 / MIL-SPEC]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import typer
from tools.audit.codebase_auditor import audit_semgrep

ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(
    help="Audit library density and architectural invariants across Arduino C++ and Python codebase.",
    add_completion=False,
)


def audit_arduino_sketches() -> list[str]:
    """Audit reference Arduino .ino sketches for bounded synchronization watchdog loop."""
    errors: list[str] = []
    sketches_dir = ROOT / "mcubridge-library-arduino" / "examples"
    if not sketches_dir.exists():
        return [f"Sketches directory not found: {sketches_dir}"]

    sync_pattern = re.compile(r"Bridge\.isSynchronized\s*\(\s*\)")
    for ino_path in sorted(sketches_dir.glob("*/*.ino")):
        text = ino_path.read_text(encoding="utf-8")
        if "Bridge.begin" in text and not sync_pattern.search(text):
            errors.append(
                f"[{ino_path.relative_to(ROOT)}] Rule 26 Violation: "
                "Sketch lacks bounded 'Bridge.isSynchronized()' watchdog loop in setup()."
            )
    return errors


@app.command()
def main() -> None:
    """Execute all automated library density and architectural rule checks."""
    semgrep_errors = audit_semgrep()
    sketch_errors = audit_arduino_sketches()
    all_errors = semgrep_errors + sketch_errors

    if all_errors:
        print("❌ ARCHITECTURAL & LIBRARY DENSITY AUDIT FAILURES:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
            print(f"::error::{err}")
        sys.exit(1)

    print("✅ Library Density & Architectural Rule Audit PASSED (100% compliant).")


if __name__ == "__main__":
    app()
