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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

app = typer.Typer(
    help="Audit library density and architectural invariants across Arduino C++ and Python codebase.",
    add_completion=False,
)


def audit_arduino_sketches() -> list[str]:
    """Audit reference Arduino .ino sketches for bounded synchronization watchdog loop."""
    sketches = sorted((ROOT / "mcubridge-library-arduino" / "examples").glob("*/*.ino"))
    sync_pattern = re.compile(r"Bridge\.isSynchronized\s*\(\s*\)")
    return [
        f"[{p.relative_to(ROOT)}] Rule 26 Violation: Sketch lacks bounded 'Bridge.isSynchronized()' in setup()."
        for p in sketches
        if "Bridge.begin" in (txt := p.read_text(encoding="utf-8")) and not sync_pattern.search(txt)
    ]


@app.command()
def main() -> None:
    """Execute all automated library density and architectural rule checks."""
    from tools.audit.codebase_auditor import audit_semgrep

    all_errors = audit_semgrep() + audit_arduino_sketches()
    if all_errors:
        print("❌ ARCHITECTURAL & LIBRARY DENSITY AUDIT FAILURES:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}\n::error::{err}", file=sys.stderr)
        sys.exit(1)

    print("✅ Library Density & Architectural Rule Audit PASSED (100% compliant).")


if __name__ == "__main__":
    app()
