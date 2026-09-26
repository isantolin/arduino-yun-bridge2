#!/usr/bin/env python3
"""Audit codebase for SIL-2/MIL-SPEC violations, suppressions, and shims."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path


import typer

ROOT = Path(__file__).resolve().parents[2]


def _audit_ast_exceptions(node: ast.AST, py_file_name: str) -> list[str]:
    """Audit AST for bare and broad Pokemon exceptions, and silent exception swallowers."""
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
                        f"Python Pokemon Exception: {py_file_name}:{node.lineno} - 'except (..., {elt.id}, ...):'"
                    )
        # Check for silent exception swallowing (no logging, assertion, or propagation in handler)
        has_call = any(isinstance(n, ast.Call) for n in ast.walk(node))
        has_raise = any(isinstance(n, ast.Raise) for n in ast.walk(node))
        has_assert = any(isinstance(n, ast.Assert) for n in ast.walk(node))
        if not (has_call or has_raise or has_assert):
            findings.append(
                f"Python Silent Exception: {py_file_name}:{node.lineno} - "
                f"exception handler silently swallows without diagnostic logging, assertion, or propagation"
            )
    return findings


def _is_time_sleep(func: ast.AST) -> bool:
    return isinstance(func, ast.Attribute) and getattr(func.value, "id", None) == "time" and func.attr == "sleep"


def _is_threading_thread(func: ast.AST) -> bool:
    return isinstance(func, ast.Attribute) and getattr(func.value, "id", None) == "threading" and func.attr == "Thread"


def _is_thread_join(func: ast.AST) -> bool:
    val_id = str(getattr(getattr(func, "value", None), "id", ""))
    return isinstance(func, ast.Attribute) and func.attr == "join" and "thread" in val_id.lower()


def _audit_ast_async_blocking(node: ast.AST, py_file_name: str) -> list[str]:
    """Audit async functions for blocking synchronous primitives and un-offloaded I/O."""
    findings: list[str] = []
    if isinstance(node, ast.AsyncFunctionDef):
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Call):
                func = subnode.func
                # Detect time.sleep() inside async function
                if _is_time_sleep(func):
                    findings.append(
                        f"Blocking Call in Async: {py_file_name}:{subnode.lineno} - "
                        f"'time.sleep()' inside async def {node.name}"
                    )
                # Detect threading.Thread() inside async function
                elif _is_threading_thread(func):
                    findings.append(
                        f"Blocking Thread in Async: {py_file_name}:{subnode.lineno} - "
                        f"'threading.Thread()' inside async def {node.name}"
                    )
                # Detect thread.join() inside async function
                elif _is_thread_join(func):
                    val_id = getattr(getattr(func, "value", None), "id", "thread")
                    findings.append(
                        f"Blocking Thread Join in Async: {py_file_name}:{subnode.lineno} - "
                        f"'{val_id}.join()' inside async def {node.name}"
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
        (
            re.compile(r"#\s*(type|pyright|mypy|pylint|flake8|ruff):"),
            "Static type / linter suppression comment (# type: / # pyright: / # mypy: / # pylint:)",
        ),
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
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.body and node.body[0].lineno == node.lineno:
                            findings.append(
                                f"Python E704 Def Statement on Same Line: {py_file.name}:{node.lineno} - "
                                f"def '{node.name}' has body on definition line"
                            )
            except SyntaxError as exc:
                findings.append(f"Python Syntax Error: {py_file.name} - {exc}")

            # 2. Line-by-line audit for Rule 4 suppressions, blank lines (E303), and shims
            consecutive_blanks = 0
            for i, line in enumerate(content.splitlines(), 1):
                clean_line = line.strip()
                if not clean_line:
                    consecutive_blanks += 1
                    if consecutive_blanks > 2:
                        findings.append(
                            f"Python E303 Too Many Blank Lines: {py_file.name}:{i} - "
                            f"{consecutive_blanks} consecutive blank lines"
                        )
                else:
                    consecutive_blanks = 0

                for pattern, desc in suppression_patterns:
                    if pattern.search(clean_line):
                        findings.append(f"Python Suppression: {py_file.name}:{i} - {desc}: '{clean_line}'")
                if passthrough_pattern.search(line):
                    findings.append(f"Python Passthrough Shim: {py_file.name}:{i} - '{clean_line}'")
                if len(line) > 120:
                    findings.append(f"Python E501 Line Too Long: {py_file.name}:{i} - {len(line)} > 120 chars")

            if content.endswith("\n\n"):
                findings.append(f"Python W391 Trailing Blank Lines at EOF: {py_file.name}")
    return findings


def audit_cpp_files() -> list[str]:
    """Audit C++ source files for manual loops, non-template wrappers, and suppressions."""
    findings: list[str] = []
    print("Auditing C++ files...")
    loop_pattern = re.compile(r"\b(for|while)\s*\(.*?\)")
    non_template_wrapper_pattern = re.compile(r"class\s+\w+Wrapper\b(?!.*template)")
    cpp_suppression_pattern = re.compile(r"//\s*(NOLINT|cppcheck-suppress|clang-diagnostic)")

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
                if cpp_suppression_pattern.search(stripped):
                    findings.append(f"C++ Suppression Found: {cpp_file.name}:{i} - '{stripped}'")
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
        if re.search(r"reportPrivateUsage\s*=\s*(?:false|['\"]none['\"])", content):
            findings.append(
                "Suppression Violation: 'reportPrivateUsage = false/none' suppression detected in pyproject.toml"
            )
        if re.search(r"executionEnvironments\s*=", content) and "reportPrivateUsage" in content:
            findings.append(
                "Suppression Violation: 'executionEnvironments' with reportPrivateUsage suppression in pyproject.toml"
            )
    return findings


def audit_proto_integrity(proto_path: Path | None = None) -> list[str]:
    """Audit tools/protocol/mcubridge.proto for dead, abandoned, or redundant definitions (Rule 35)."""
    findings: list[str] = []
    print("Auditing Protobuf definitions...")
    target_path = proto_path if proto_path is not None else ROOT / "tools" / "protocol" / "mcubridge.proto"
    if not target_path.exists():
        findings.append(f"Protobuf File Missing: {target_path} not found")
        return findings

    content = target_path.read_text(encoding="utf-8")

    # 1. Obsolete options / messages
    if "data_formats" in content:
        findings.append("Protobuf Dead Block: 'data_formats' is obsolete and must be removed")
    if "DataFormats" in content:
        findings.append("Protobuf Dead Message: 'DataFormats' is obsolete and must be removed")

    # 2. Dead string options in Handshake
    dead_handshake_fields = [
        "tag_algorithm",
        "tag_description",
        "hkdf_algorithm",
        "nonce_format_description",
        "aead_algorithm",
        "aead_description",
    ]
    for field in dead_handshake_fields:
        if re.search(rf"\b{field}\s*:", content):
            findings.append(f"Protobuf Dead Field: handshake.{field} is purely decorative and must be removed")

    # 3. Dead constants in Constants
    dead_constants = [
        "default_serial_fallback_threshold",
        "cloud_expiry_shell",
        "cloud_expiry_default",
    ]
    for const in dead_constants:
        if re.search(rf"\b{const}\s*:", content):
            findings.append(f"Protobuf Dead Constant: constants.{const} has zero usages and must be removed")

    return findings


def audit_linters() -> list[str]:
    """Audit codebase with flake8 and ruff to detect any linter or E501 errors."""
    findings: list[str] = []
    print("Auditing linters (flake8 & ruff)...")

    # 1. Flake8
    res_flake = subprocess.run(["flake8"], cwd=ROOT, capture_output=True, text=True, check=False)
    if res_flake.returncode != 0:
        for line in res_flake.stdout.splitlines():
            line_str = line.strip()
            if line_str:
                findings.append(f"Flake8 Violation: {line_str}")

    # 2. Ruff
    res_ruff = subprocess.run(
        ["ruff", "check", "mcubridge", "mcubridge-client-examples", "mcubridge-gateway", "tools"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    if res_ruff.returncode != 0:
        for line in res_ruff.stdout.splitlines():
            line_str = line.strip()
            if line_str and not line_str.startswith("Found "):
                findings.append(f"Ruff Violation: {line_str}")

    return findings


app = typer.Typer(help="Audit codebase for SIL-2/MIL-SPEC violations and shims.", add_completion=False)


@app.command()
def main() -> None:
    """Execute python, C++, config, and protobuf compliance audits."""
    py_findings = audit_python_files()
    cpp_findings = audit_cpp_files()
    cfg_findings = audit_config_suppressions()
    proto_findings = audit_proto_integrity()
    linter_findings = audit_linters()

    all_findings = py_findings + cpp_findings + cfg_findings + proto_findings + linter_findings

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
