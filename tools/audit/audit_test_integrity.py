#!/usr/bin/env python3
"""Automated Test Integrity Auditor (SIL-2 / MIL-SPEC).

Enforces Rule 11, Rule 17, and Rule 18 compliance:
1. 100% of Python test functions must assert concrete state or emitted frames.
2. 100% of Unity C++ test cases must call TEST_ASSERT* macros.
Tautology and discarded call checks are delegated to Semgrep (.semgrep.yml) and Ruff (B018, PT).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Annotated

import typer

ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(
    help="Audit Python and C++ test suites for Rule 17/18 zero-assertion violations.",
    add_completion=False,
)

MOCK_ASSERT_NAMES = {
    "assert_called",
    "assert_called_once",
    "assert_called_with",
    "assert_called_once_with",
    "assert_any_call",
    "assert_has_calls",
    "assert_not_called",
    "assert_awaited",
    "assert_awaited_once",
    "assert_awaited_with",
    "assert_awaited_once_with",
    "assert_not_awaited",
}


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function is decorated with @pytest.fixture."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if getattr(target, "id", None) == "fixture" or getattr(target, "attr", None) == "fixture":
            return True
    return False


def _is_state_machine_runner(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function is a Hypothesis RuleBasedStateMachine runner wrapper."""
    dump = ast.dump(node)
    return "StateMachine" in dump and any(
        k in dump for k in ("default_runner", "_RUN_STATE_MACHINE", "run_state_machine_as_test")
    )


def _inspect_node_assertions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count number of substantive assertions in a test function."""
    count = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            count += 1
        elif isinstance(sub, ast.With):
            for item in sub.items:
                fn = item.context_expr.func if isinstance(item.context_expr, ast.Call) else None
                if getattr(fn, "attr", None) == "raises" or getattr(fn, "id", None) == "raises":
                    count += 1
        elif isinstance(sub, ast.Call) and getattr(sub.func, "attr", None) in MOCK_ASSERT_NAMES:
            count += 1
    return count


def audit_python_tests() -> list[str]:
    """Scan all test files in python test directories."""
    findings: list[str] = []
    print("Auditing Python test integrity...")
    search_dirs = [
        ROOT / "mcubridge" / "tests",
        ROOT / "mcubridge-gateway" / "tests",
        ROOT / "mcubridge-client-examples" / "tests",
    ]
    for d in search_dirs:
        if not d.exists():
            continue
        for py_path in sorted(d.glob("**/test_*.py")):
            try:
                tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            except (SyntaxError, UnicodeDecodeError) as exc:
                findings.append(f"[{py_path.relative_to(ROOT)}] Parse Error: {exc}")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not node.name.startswith("test_") or _is_fixture(node) or _is_state_machine_runner(node):
                    continue
                if _inspect_node_assertions(node) == 0:
                    findings.append(
                        f"[{py_path.relative_to(ROOT)}:{node.lineno}] Rule 17/18 Violation: "
                        f"Function '{node.name}' has ZERO assertions."
                    )
    return findings


def audit_cpp_tests() -> list[str]:
    """Scan all C++ test files in Arduino library test directories."""
    findings: list[str] = []
    print("Auditing C++ test integrity...")
    test_dir = ROOT / "mcubridge-library-arduino" / "tests"
    if not test_dir.exists():
        return findings
    ignored = {"bridge_emulator.cpp", "bridge_control_emulator.cpp", "bridge_test_global.cpp"}
    for cpp_path in sorted(test_dir.glob("*.cpp")):
        if cpp_path.name in ignored:
            continue
        lines: list[str] = cpp_path.read_text(encoding="utf-8").splitlines()
        current_func: str | None = None
        brace_depth: int = 0
        func_lines: list[str] = []
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("void test_") and "(" in line:
                current_func = line.strip().split("(")[0].replace("void ", "").strip()
                brace_depth = 0
                func_lines = []
            if current_func:
                func_lines.append(line)
                brace_depth += line.count("{") - line.count("}")
                if brace_depth == 0 and "{" in "".join(func_lines):
                    if not any(k in "".join(func_lines) for k in ("TEST_ASSERT", "assert(")):
                        findings.append(
                            f"[{cpp_path.relative_to(ROOT)}:{i}] Rule 17/18 Violation: "
                            f"C++ '{current_func}' has ZERO assertions."
                        )
                    current_func = None
    return findings


@app.command()
def main(
    strict: Annotated[
        bool, typer.Option("--strict/--no-strict", help="Exit with code 1 if integrity violations are found.")
    ] = True,
) -> None:
    """Execute automated test integrity audit across Python and C++ test suites."""
    all_findings = audit_python_tests() + audit_cpp_tests()
    print("\n--- TEST INTEGRITY AUDIT RESULTS ---")
    if not all_findings:
        print("✅ 100% of test functions have substantive assertions and pass Rule 17/18 integrity gates.")
        return

    print(f"❌ Found {len(all_findings)} test integrity violation(s):")
    for f in all_findings:
        print(f"  {f}\n::error::{f}")
    if strict:
        sys.exit(1)


if __name__ == "__main__":
    app()
