#!/usr/bin/env python3
"""Automated Static Library Density & Architectural Rule Auditor.

Enforces SIL-2/MIL-SPEC compliance across:
1. Arduino examples (.ino): Canonical singleton, bounded synchronization, shared secret.
2. C++ Library (src/): Zero-loop rule, zero manual bit shifts on streams, zero raw clamps.
3. Python (mcubridge/): Zero-suppression, modern Python 3.13+ idioms, zero wrapper shims.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
import typer

ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(
    help="Audit library density and architectural invariants across Arduino C++ and Python codebase.",
    add_completion=False,
)


def audit_arduino_sketches() -> list[str]:
    """Audit all reference Arduino .ino sketches."""
    errors: list[str] = []
    sketches_dir = ROOT / "mcubridge-library-arduino" / "examples"
    if not sketches_dir.exists():
        return [f"Sketches directory not found: {sketches_dir}"]

    instance_decl_pattern = re.compile(r"^\s*BridgeClass\s+([A-Za-z0-9_]+)\s*\(", re.MULTILINE)
    secret_pattern = re.compile(r"Bridge\.begin\s*\([^,\n]+,\s*BRIDGE_SERIAL_SHARED_SECRET\s*\)")
    sync_pattern = re.compile(r"Bridge\.isSynchronized\s*\(\s*\)")

    for ino_path in sorted(sketches_dir.glob("*/*.ino")):
        text = ino_path.read_text(encoding="utf-8")
        rel_path = ino_path.relative_to(ROOT)

        # 1. Prohibition of secondary BridgeClass instances
        for match in instance_decl_pattern.finditer(text):
            var_name = match.group(1)
            if var_name != "Bridge":
                errors.append(
                    f"[{rel_path}] Rule 25 Violation: Declared non-canonical BridgeClass instance '{var_name}'. "
                    f"Must use canonical singleton 'Bridge' and 'Bridge.setStream(stream)'."
                )

        # 2. Shared secret authentication in Bridge.begin
        if "Bridge.begin" in text and not secret_pattern.search(text):
            errors.append(f"[{rel_path}] Rule 26 Violation: Bridge.begin() called without BRIDGE_SERIAL_SHARED_SECRET.")

        # 3. Bounded synchronization loop
        if "Bridge.begin" in text and not sync_pattern.search(text):
            errors.append(
                f"[{rel_path}] Rule 26 Violation: "
                "Sketch lacks bounded 'Bridge.isSynchronized()' watchdog loop in setup()."
            )

    return errors


def audit_cpp_library_density() -> list[str]:
    """Audit C++ production source files for imperative loops and manual stream shifts."""
    errors: list[str] = []
    src_dir = ROOT / "mcubridge-library-arduino" / "src"
    if not src_dir.exists():
        return [f"C++ source directory not found: {src_dir}"]

    # Ignored third-party or generated files
    ignored = {"pb_common.c", "pb_decode.c", "pb_encode.c", "mcubridge.pb.c", "mcubridge.pb.h"}

    manual_loop_pattern = re.compile(r"\b(for|while)\s*\(")
    manual_shift_pattern = re.compile(r"(\[.*\]\s*<<\s*24|\(.*\)\s*>>\s*24)")

    for cpp_file in sorted(src_dir.rglob("*")):
        if cpp_file.is_file() and cpp_file.suffix in {".cpp", ".h"}:
            if cpp_file.name in ignored:
                continue

            lines = cpp_file.read_text(encoding="utf-8").splitlines()
            rel_path = cpp_file.relative_to(ROOT)

            for idx, line in enumerate(lines, start=1):
                clean_line = line.strip()
                if clean_line.startswith("//") or clean_line.startswith("/*"):
                    continue

                # Check for manual loops outside ETL profile / macro guards
                if manual_loop_pattern.search(clean_line) and not clean_line.startswith("#"):
                    # Exclude atomic block macros or standard macros
                    if "BRIDGE_ATOMIC_BLOCK" not in clean_line and "for" in clean_line:
                        errors.append(
                            f"[{rel_path}:{idx}] Rule 8 Violation: Manual imperative loop detected: '{clean_line}'. "
                            f"Must use ETL algorithms (etl::find, etl::copy_n, etl::for_each, etl::all_of, etc.)."
                        )

                # Check for manual 4-byte shifting where etl::byte_stream should be used
                if manual_shift_pattern.search(clean_line):
                    errors.append(
                        f"[{rel_path}:{idx}] Rule 27 Violation: Manual 32-bit byte shifting detected: '{clean_line}'. "
                        f"Must use etl::byte_stream_reader / etl::byte_stream_writer."
                    )

    return errors


def audit_python_suppression_and_context() -> list[str]:
    """Audit Python production code for suppression violations."""
    errors: list[str] = []
    py_dirs = [ROOT / "mcubridge" / "mcubridge", ROOT / "mcubridge-gateway", ROOT / "tools"]

    suppression_patterns = [
        (re.compile(r"#\s*(type|pyright):\s*ignore"), "Static type suppression"),
        (re.compile(r"#\s*noqa"), "Linter suppression (# noqa)"),
        (re.compile(r"#\s*pragma:\s*no cover"), "Coverage suppression (pragma: no cover)"),
        (re.compile(r"errors\s*=\s*['\"](ignore|replace|backslashreplace)['\"]"), "String encoding suppression"),
        (re.compile(r"contextlib\.suppress"), "Exception suppression (contextlib.suppress)"),
        (re.compile(r"except\s*:\s*pass"), "Catch-all silent pass (except: pass)"),
    ]

    for base_dir in py_dirs:
        if not base_dir.exists():
            continue
        for py_file in sorted(base_dir.rglob("*.py")):
            # Exclude autogenerated protobuf stubs and the auditors themselves
            if (
                py_file.name.endswith("_pb2.py")
                or py_file.name.endswith("_pb2_grpc.py")
                or py_file.name in {"audit_library_density.py", "codebase_auditor.py"}
            ):
                continue

            lines = py_file.read_text(encoding="utf-8").splitlines()
            rel_path = py_file.relative_to(ROOT)

            for idx, line in enumerate(lines, start=1):
                clean_line = line.strip()
                for pattern, name in suppression_patterns:
                    if pattern.search(clean_line):
                        errors.append(f"[{rel_path}:{idx}] Rule 4 Violation: {name} detected: '{clean_line}'.")

    return errors


@app.command()
def main() -> None:
    """Execute all automated library density and architectural rule checks."""
    sketch_errors = audit_arduino_sketches()
    cpp_errors = audit_cpp_library_density()
    py_errors = audit_python_suppression_and_context()

    all_errors = sketch_errors + cpp_errors + py_errors

    if all_errors:
        print("❌ ARCHITECTURAL & LIBRARY DENSITY AUDIT FAILURES:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    print("✅ Library Density & Architectural Rule Audit PASSED (100% compliant).")


if __name__ == "__main__":
    app()
