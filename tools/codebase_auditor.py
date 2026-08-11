#!/usr/bin/env python3
"""Audit codebase for SIL-2/MIL-SPEC violations, suppressions, and shims."""

from __future__ import annotations

from pathlib import Path
import re
import typer

ROOT = Path(__file__).resolve().parent.parent


def audit_python_files() -> list[str]:
    """Audit Python source files for Pokemon exceptions and passthrough shims."""
    findings: list[str] = []
    print("Auditing Python files...")
    pokemon_pattern = re.compile(r"except\s*:\s*pass|except\s+Exception\s*:\s*pass|errors\s*=\s*['\"]ignore['\"]")
    passthrough_pattern = re.compile(
        r"def\s+(\w+)\(self,\s*\*args,\s*\*\*kwargs\):\s*return\s+self\.\w+\(\*args,\s*\*\*kwargs\)"
    )

    for py_file in (ROOT / "mcubridge").rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for i, line in enumerate(content.splitlines(), 1):
            if pokemon_pattern.search(line):
                findings.append(f"Python Suppression: {py_file.name}:{i} - '{line.strip()}'")
            if passthrough_pattern.search(line):
                findings.append(f"Python Passthrough Shim: {py_file.name}:{i} - '{line.strip()}'")
    return findings


def audit_cpp_files() -> list[str]:
    """Audit C++ source files for manual loops and non-template wrappers."""
    findings: list[str] = []
    print("Auditing C++ files...")
    loop_pattern = re.compile(r"\b(for|while)\s*\(.*?\)")
    non_template_wrapper_pattern = re.compile(r"class\s+\w+Wrapper\b(?!.*template)")

    cpp_dir = ROOT / "mcubridge-library-arduino" / "src"
    for cpp_file in cpp_dir.rglob("*"):
        if cpp_file.suffix not in (".h", ".cpp"):
            continue
        if any(ignored in cpp_file.name for ignored in ("mcubridge.pb", "pb_encode", "pb_decode", "pb_common")):
            continue

        content = cpp_file.read_text(encoding="utf-8")
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("//", "/*", "*")):
                continue
            if loop_pattern.search(line):
                findings.append(f"C++ Loop Found: {cpp_file.name}:{i} - '{stripped}'")
            if non_template_wrapper_pattern.search(line):
                findings.append(f"C++ Non-Template Wrapper Found: {cpp_file.name}:{i} - '{stripped}'")
    return findings


app = typer.Typer(help="Audit codebase for SIL-2/MIL-SPEC violations and shims.", add_completion=False)


@app.command()
def main() -> None:
    """Execute python and C++ compliance audits."""
    py_findings = audit_python_files()
    cpp_findings = audit_cpp_files()

    print("\n--- RESULTS ---")
    if not py_findings and not cpp_findings:
        print("No violations or shims found! The codebase is 100% clean and compliant.")
    else:
        for f in py_findings + cpp_findings:
            print(f)


if __name__ == "__main__":
    app()
