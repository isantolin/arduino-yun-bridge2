#!/usr/bin/env python3
"""Audit codebase for SIL-2/MIL-SPEC violations, suppressions, and shims."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
import typer

ROOT = Path(__file__).resolve().parents[2]


def _audit_ast_exceptions(node: ast.AST, py_file_name: str) -> list[str]:
    """Audit AST for bare and broad Pokemon exceptions."""
    findings: list[str] = []
    if isinstance(node, ast.ExceptHandler):
        if node.type is None:
            findings.append(f"Python Pokemon Exception: {py_file_name}:{node.lineno} - bare 'except:'")
        elif isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
            findings.append(f"Python Pokemon Exception: {py_file_name}:{node.lineno} - 'except {node.type.id}:'")
        elif isinstance(node.type, ast.Tuple):
            for elt in node.type.elts:
                if isinstance(elt, ast.Name) and elt.id in ("Exception", "BaseException"):
                    findings.append(
                        f"Python Pokemon Exception: {py_file_name}:{node.lineno} - " f"'except (..., {elt.id}, ...):'"
                    )
    return findings


def _audit_ast_async_blocking(node: ast.AST, py_file_name: str) -> list[str]:
    """Audit async functions for blocking synchronous primitives and un-offloaded I/O."""
    findings: list[str] = []
    if isinstance(node, ast.AsyncFunctionDef):
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Call):
                # Detect time.sleep() inside async function
                if (
                    isinstance(subnode.func, ast.Attribute)
                    and isinstance(subnode.func.value, ast.Name)
                    and subnode.func.value.id == "time"
                    and subnode.func.attr == "sleep"
                ):
                    findings.append(
                        f"Blocking Call in Async: {py_file_name}:{subnode.lineno} - "
                        f"'time.sleep()' inside async def {node.name}"
                    )
                # Detect threading.Thread() inside async function
                elif (
                    isinstance(subnode.func, ast.Attribute)
                    and isinstance(subnode.func.value, ast.Name)
                    and subnode.func.value.id == "threading"
                    and subnode.func.attr == "Thread"
                ):
                    findings.append(
                        f"Blocking Thread in Async: {py_file_name}:{subnode.lineno} - "
                        f"'threading.Thread()' inside async def {node.name}"
                    )
                # Detect thread.join() inside async function
                elif (
                    isinstance(subnode.func, ast.Attribute)
                    and subnode.func.attr == "join"
                    and isinstance(subnode.func.value, ast.Name)
                    and "thread" in subnode.func.value.id.lower()
                ):
                    findings.append(
                        f"Blocking Thread Join in Async: {py_file_name}:{subnode.lineno} - "
                        f"'{subnode.func.value.id}.join()' inside async def {node.name}"
                    )
                # Detect direct call to _vacuum_lmdb_env() without anyio.to_thread.run_sync
                elif isinstance(subnode.func, ast.Name) and subnode.func.id == "_vacuum_lmdb_env":
                    findings.append(
                        f"Blocking Compaction in Async: {py_file_name}:{subnode.lineno} - "
                        f"direct call to '_vacuum_lmdb_env()' inside async def {node.name}; "
                        f"must be offloaded via anyio.to_thread.run_sync"
                    )
    return findings


def audit_python_files() -> list[str]:
    """Audit Python source files for Pokemon exceptions, suppressions, and passthrough shims."""
    findings: list[str] = []
    print("Auditing Python files...")

    suppression_patterns = [
        (re.compile(r"#\s*(type|pyright):\s*ignore"), "Static type suppression (# type: ignore)"),
        (re.compile(r"#\s*noqa"), "Linter suppression (# noqa)"),
        (re.compile(r"#\s*pragma:\s*no cover"), "Coverage suppression (pragma: no cover)"),
        (re.compile(r"errors\s*=\s*['\"](ignore|replace|backslashreplace)['\"]"), "String encoding suppression"),
        (re.compile(r"contextlib\.suppress"), "Exception suppression (contextlib.suppress)"),
        (re.compile(r"@(?:typing\.)?no_type_check"), "Typecheck suppression (@no_type_check)"),
    ]

    passthrough_pattern = re.compile(
        r"def\s+(\w+)\(self,\s*\*args,\s*\*\*kwargs\):\s*return\s+self\.\w+\(\*args,\s*\*\*kwargs\)"
    )

    py_dirs = [
        ROOT / "mcubridge",
        ROOT / "mcubridge-client-examples",
        ROOT / "mcubridge-gateway",
        ROOT / "tools",
    ]

    for base_dir in py_dirs:
        for py_file in base_dir.rglob("*.py"):
            if "_pb2" in py_file.name or py_file.name in {"audit_library_density.py", "codebase_auditor.py"}:
                continue
            content = py_file.read_text(encoding="utf-8")

            # 1. AST audit for Pokemon exceptions, silent handlers, and blocking async calls
            try:
                tree = ast.parse(content, filename=str(py_file))
                for node in ast.walk(tree):
                    findings.extend(_audit_ast_exceptions(node, py_file.name))
                    findings.extend(_audit_ast_async_blocking(node, py_file.name))
            except SyntaxError as exc:
                findings.append(f"Python Syntax Error: {py_file.name} - {exc}")

            # 2. Line-by-line audit for Rule 4 suppressions and shims
            for i, line in enumerate(content.splitlines(), 1):
                clean_line = line.strip()
                for pattern, desc in suppression_patterns:
                    if pattern.search(clean_line):
                        findings.append(f"Python Suppression: {py_file.name}:{i} - {desc}: '{clean_line}'")
                if passthrough_pattern.search(line):
                    findings.append(f"Python Passthrough Shim: {py_file.name}:{i} - '{clean_line}'")
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


def audit_config_suppressions() -> list[str]:
    """Audit project configuration files for linter/typechecker suppressions."""
    findings: list[str] = []
    print("Auditing Configuration files...")
    pyproject = ROOT / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8")
        if "per-file-ignores" in content:
            findings.append("Suppression Violation: 'per-file-ignores' detected in pyproject.toml")
    return findings


app = typer.Typer(help="Audit codebase for SIL-2/MIL-SPEC violations and shims.", add_completion=False)


@app.command()
def main() -> None:
    """Execute python, C++, and config compliance audits."""
    py_findings = audit_python_files()
    cpp_findings = audit_cpp_files()
    cfg_findings = audit_config_suppressions()

    all_findings = py_findings + cpp_findings + cfg_findings

    print("\n--- RESULTS ---")
    if not all_findings:
        print("No violations or shims found! The codebase is 100% clean and compliant.")
    else:
        for f in all_findings:
            print(f)
            print(f"::error::{f}")
        sys.exit(1)


if __name__ == "__main__":
    app()
