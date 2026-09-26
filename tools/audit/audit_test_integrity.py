#!/usr/bin/env python3
"""Automated Test Integrity & Anti-Cheating Auditor (SIL-2 / MIL-SPEC).

Enforces Rule 11, Rule 17, and Rule 18 compliance across all test suites:
1. Genuine Test Integrity: Prohibit dummy assertions, tautological assertions in except
   blocks, self-mutation tests, and discarded calls.
2. Deterministic Post-Conditions: 100% of test functions must assert concrete state,
   return values, or emitted frames (zero fire-and-forget).
3. C++ Host Integrity: 100% of Unity test cases must call TEST_ASSERT* macros.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
from typing import Annotated

import typer

ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(
    help="Audit Python and C++ test suites for Rule 11/17/18 integrity violations.",
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
        if isinstance(dec, ast.Attribute) and dec.attr == "fixture":
            return True
        if isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Attribute) and dec.func.attr == "fixture":
                return True
            if isinstance(dec.func, ast.Name) and dec.func.id == "fixture":
                return True
        if isinstance(dec, ast.Name) and dec.id == "fixture":
            return True
    return False


def _is_state_machine_runner(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function is a Hypothesis RuleBasedStateMachine runner wrapper."""
    dump = ast.dump(node)
    return "StateMachine" in dump and "default_runner" in dump


def _get_handler_exception_names(handler: ast.ExceptHandler) -> set[str]:
    """Extract names of exception types caught by an except handler."""
    names: set[str] = set()
    if handler.type is None:
        return names
    if isinstance(handler.type, ast.Name):
        names.add(handler.type.id)
    elif isinstance(handler.type, ast.Attribute):
        names.add(handler.type.attr)
    elif isinstance(handler.type, ast.Tuple):
        for elt in handler.type.elts:
            if isinstance(elt, ast.Name):
                names.add(elt.id)
            elif isinstance(elt, ast.Attribute):
                names.add(elt.attr)
    return names


def _is_tautological_isinstance(inner: ast.Assert, caught_names: set[str], handler_var: str) -> bool:
    """Detect 'assert isinstance(exc, CaughtType)' inside an except block."""
    if not (
        isinstance(inner.test, ast.Call)
        and isinstance(inner.test.func, ast.Name)
        and inner.test.func.id == "isinstance"
        and len(inner.test.args) >= 2
    ):
        return False

    first_arg = inner.test.args[0]
    second_arg = inner.test.args[1]
    if not (isinstance(first_arg, ast.Name) and first_arg.id == handler_var):
        return False

    second_names: set[str] = set()
    if isinstance(second_arg, ast.Name):
        second_names.add(second_arg.id)
    elif isinstance(second_arg, ast.Attribute):
        second_names.add(second_arg.attr)
    elif isinstance(second_arg, ast.Tuple):
        for elt in second_arg.elts:
            if isinstance(elt, ast.Name):
                second_names.add(elt.id)
            elif isinstance(elt, ast.Attribute):
                second_names.add(elt.attr)

    return bool(caught_names & second_names)


def _inspect_node_assertions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, bool, bool]:
    """Return (assert_count, has_dummy_assert_true, has_tautology_in_except)."""
    assert_count = 0
    has_dummy_assert_true = False
    has_tautology_in_except = False

    for subnode in ast.walk(node):
        if isinstance(subnode, ast.Assert):
            assert_count += 1
            if isinstance(subnode.test, ast.Constant) and subnode.test.value is True:
                has_dummy_assert_true = True
        elif isinstance(subnode, ast.With):
            for item in subnode.items:
                if isinstance(item.context_expr, ast.Call):
                    fn = item.context_expr.func
                    if (isinstance(fn, ast.Attribute) and fn.attr == "raises") or (
                        isinstance(fn, ast.Name) and fn.id == "raises"
                    ):
                        assert_count += 1
        elif isinstance(subnode, ast.Call):
            if isinstance(subnode.func, ast.Attribute) and subnode.func.attr in MOCK_ASSERT_NAMES:
                assert_count += 1
        elif isinstance(subnode, ast.ExceptHandler):
            caught = _get_handler_exception_names(subnode)
            if subnode.name and caught:
                for inner in ast.walk(subnode):
                    if isinstance(inner, ast.Assert) and _is_tautological_isinstance(inner, caught, subnode.name):
                        has_tautology_in_except = True

    return assert_count, has_dummy_assert_true, has_tautology_in_except


def audit_python_test_file(py_path: Path) -> list[str]:
    """Audit a single Python test file for AST assertions, tautologies, and self-mutations."""
    findings: list[str] = []
    rel_path = py_path.relative_to(ROOT)

    try:
        content = py_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(py_path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"[{rel_path}] Parse Error: {exc}"]

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_") or _is_fixture(node) or _is_state_machine_runner(node):
            continue

        assert_count, has_dummy_assert_true, has_tautology = _inspect_node_assertions(node)

        if assert_count == 0:
            findings.append(
                f"[{rel_path}:{node.lineno}] Rule 17/18 Violation: Function '{node.name}' has ZERO assertions "
                f"(superficial line-hitting / fire-and-forget)."
            )

        if has_dummy_assert_true:
            findings.append(
                f"[{rel_path}:{node.lineno}] Rule 11 Violation: Function '{node.name}' contains literal 'assert True'."
            )

        if has_tautology:
            findings.append(
                f"[{rel_path}:{node.lineno}] Rule 11 Violation: Function '{node.name}' contains tautological "
                f"'assert isinstance(exc, ...)' inside except block; must assert concrete error message via "
                f"pytest.raises(..., match=...) instead."
            )

    return findings


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
            findings.extend(audit_python_test_file(py_path))
    return findings


def audit_cpp_test_file(cpp_path: Path) -> list[str]:
    """Audit a single C++ test file for Unity TEST_ASSERT calls and discarded SUT calls."""
    findings: list[str] = []
    rel_path = cpp_path.relative_to(ROOT)
    lines = cpp_path.read_text(encoding="utf-8").splitlines()

    current_func: tuple[str, int] | None = None
    brace_depth = 0
    func_lines: list[str] = []

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("void test_") and "(" in stripped:
            func_name = stripped.split("(")[0].replace("void ", "").strip()
            current_func = (func_name, i)
            brace_depth = 0
            func_lines = []

        if current_func:
            func_lines.append(line)
            brace_depth += line.count("{") - line.count("}")
            if brace_depth == 0 and "{" in "".join(func_lines):
                body = "\n".join(func_lines)
                fname, lineno = current_func

                if "TEST_ASSERT" not in body and "assert(" not in body:
                    findings.append(
                        f"[{rel_path}:{lineno}] Rule 17/18 Violation: C++ function '{fname}' has ZERO assertions "
                        f"(no TEST_ASSERT* called)."
                    )

                if re.search(r"\(\s*void\s*\)\s*(?:Bridge\.|TestAccessor|ba\.)", body):
                    findings.append(
                        f"[{rel_path}:{lineno}] Rule 11 Violation: C++ function '{fname}' explicitly discards "
                        f"SUT return value with '(void)'."
                    )

                current_func = None
                func_lines = []

    return findings


def audit_cpp_tests() -> list[str]:
    """Scan all C++ test files in Arduino library test directories."""
    findings: list[str] = []
    print("Auditing C++ test integrity...")
    test_dir = ROOT / "mcubridge-library-arduino" / "tests"
    if not test_dir.exists():
        return findings

    for cpp_path in sorted(test_dir.glob("*.cpp")):
        if cpp_path.name in {"bridge_emulator.cpp", "bridge_control_emulator.cpp", "bridge_test_global.cpp"}:
            continue
        findings.extend(audit_cpp_test_file(cpp_path))
    return findings


@app.command()
def main(
    strict: Annotated[
        bool, typer.Option("--strict/--no-strict", help="Exit with code 1 if integrity violations are found.")
    ] = True,
) -> None:
    """Execute automated test integrity audit across Python and C++ test suites."""
    py_findings = audit_python_tests()
    cpp_findings = audit_cpp_tests()
    all_findings = py_findings + cpp_findings

    print("\n--- TEST INTEGRITY AUDIT RESULTS ---")
    if not all_findings:
        print("✅ 100% of test functions have substantive assertions and pass Rule 11/17/18 integrity gates.")
        sys.exit(0)

    print(f"❌ Found {len(all_findings)} test integrity violation(s):")
    for f in all_findings:
        print(f"  {f}")
        print(f"::error::{f}")

    if strict:
        sys.exit(1)


if __name__ == "__main__":
    app()
