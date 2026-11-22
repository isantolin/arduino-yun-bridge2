#!/usr/bin/env python3
"""Automated Test Integrity Auditor (SIL-2 / MIL-SPEC).

Enforces Rule 11, Rule 17, and Rule 18 compliance:
1. 100% of Python test functions must assert concrete state or emitted frames.
C++ test assertion (sil2-ban-cpp-test-without-assert), tautology, and discarded call
checks are delegated to Semgrep (.semgrep.yml) and Ruff (B018, PT).
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator, Sequence
import io
from pathlib import Path
import re
import sys
import tokenize
from typing import Annotated

import typer

ROOT = Path(__file__).resolve().parents[2]

_DOTTED_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_DYNAMIC_DISPATCH_PREFIXES = (
    "__",
    "ubus_handle_",
    "on_mcu_",
    "_on_mcu_",
    "on_enter_",
    "on_exit_",
)

IGNORED_IDENTIFIERS = {
    "min",
    "max",
    "get",
    "set",
    "reset",
    "begin",
    "end",
    "clear",
    "write",
    "read",
    "run",
    "close",
    "open",
    "start",
    "stop",
    "connect",
    "disconnect",
    "process",
    "update",
    "build",
    "parse",
    "format",
    "render",
    "create",
    "send",
    "receive",
    "flush",
}

ALLOWLIST_PY_HELPERS = {
    # Documented public GatewayClient SDK methods (ADR-0007 / ADR-0008) not yet exercised
    # by a standalone script under mcubridge-client-examples/examples/.
    "push_file_to_device",
    "fetch_file_from_device",
    "read_mcu_eeprom",
    "write_mcu_eeprom",
}


app = typer.Typer(
    help="Audit Python test suites for Rule 17/18 assertions and Rule 5 test-only hooks.",
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


def _iter_python_files(dirs: Sequence[Path]) -> list[Path]:
    """Collect sorted non-generated Python files under the given directories."""
    files: list[Path] = []
    for directory in dirs:
        if not directory.exists():
            continue
        files.extend(
            path
            for path in sorted(directory.rglob("*.py"))
            if not path.name.endswith(("_pb2.py", "_grpc.py", "_pb2.pyi"))
        )
    return files


def _collect_docstring_nodes(tree: ast.AST) -> set[ast.AST]:
    """Collect AST Constant nodes that serve as module, class, or function docstrings."""
    docstrings: set[ast.AST] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            docstrings.add(node.body[0].value)
    return docstrings


def _count_code_identifiers(
    content: str,
    *,
    tree: ast.AST | None = None,
    include_dispatch_strings: bool = False,
) -> Counter[str]:
    """Count Python identifier tokens (excluding comments/docstrings) and optional dispatch strings."""
    counts: Counter[str] = Counter(
        tok.string for tok in tokenize.generate_tokens(io.StringIO(content).readline) if tok.type == tokenize.NAME
    )
    if include_dispatch_strings:
        parsed_tree = tree if tree is not None else ast.parse(content)
        docstrings = _collect_docstring_nodes(parsed_tree)
        for node in ast.walk(parsed_tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node not in docstrings
                and _DOTTED_IDENTIFIER_RE.fullmatch(node.value)
            ):
                counts.update(node.value.split("."))
    return counts


def _iter_module_and_class_defs(
    body: Sequence[ast.stmt],
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    """Yield module-level and class-level functions, methods, and classes."""
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            yield stmt
            yield from _iter_module_and_class_defs(stmt.body)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield stmt


def audit_test_only_hooks(root: Path | None = None) -> list[str]:
    """Scan production code for classes, methods, and functions called exclusively from tests (Rule 5)."""
    findings: list[str] = []
    print("Auditing test-only hooks and dead backdoors (Rule 5)...")
    target_root = root or ROOT

    py_prod_def_files = _iter_python_files(
        (
            target_root / "mcubridge" / "mcubridge",
            target_root / "mcubridge-client-examples" / "mcubridge_client",
        )
    )
    py_prod_consumer_files = _iter_python_files(
        (
            target_root / "mcubridge-client-examples" / "examples",
            target_root / "mcubridge" / "scripts",
        )
    )
    py_test_files = _iter_python_files(
        (
            target_root / "mcubridge" / "tests",
            target_root / "mcubridge-client-examples" / "tests",
        )
    )

    parsed_prod_defs: list[tuple[Path, ast.Module, Counter[str]]] = []
    total_prod_counts: Counter[str] = Counter()
    for pfile in py_prod_def_files:
        try:
            content = pfile.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(pfile))
            file_counts = _count_code_identifiers(content, tree=tree, include_dispatch_strings=True)
        except (OSError, SyntaxError, UnicodeDecodeError, tokenize.TokenError):
            continue
        parsed_prod_defs.append((pfile, tree, file_counts))
        total_prod_counts += file_counts

    for cfile in py_prod_consumer_files:
        try:
            content = cfile.read_text(encoding="utf-8")
            total_prod_counts += _count_code_identifiers(content, include_dispatch_strings=True)
        except (OSError, SyntaxError, UnicodeDecodeError, tokenize.TokenError):
            continue

    total_test_counts: Counter[str] = Counter()
    for tfile in py_test_files:
        try:
            content = tfile.read_text(encoding="utf-8")
            total_test_counts += _count_code_identifiers(content, include_dispatch_strings=False)
        except (OSError, SyntaxError, UnicodeDecodeError, tokenize.TokenError):
            continue

    for pfile, tree, file_counts in parsed_prod_defs:
        for node in _iter_module_and_class_defs(tree.body):
            name = node.name
            if (
                name.startswith(_DYNAMIC_DISPATCH_PREFIXES)
                or name in IGNORED_IDENTIFIERS
                or len(name) < 4
                or name in ALLOWLIST_PY_HELPERS
            ):
                continue

            prod_count = total_prod_counts[name]
            file_count = file_counts[name]
            test_count = total_test_counts[name]

            if prod_count == file_count == 1 and test_count > 0:
                kind = "class" if isinstance(node, ast.ClassDef) else "function/method"
                findings.append(
                    f"[{pfile.relative_to(target_root)}:{node.lineno}] Rule 5 Violation: "
                    f"Production {kind} '{name}' is only invoked from test suites."
                )

    return findings


def audit_boolean_identity_assertions() -> list[str]:
    """Scan test suites for repeated boolean identity assertions ('is True' / 'is False')
    that trigger Pyrefly unnecessary-comparison narrowing errors.
    """
    findings: list[str] = []
    print("Auditing boolean identity comparisons in assertions...")
    search_dirs = [
        ROOT / "mcubridge" / "tests",
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
                seen_asserts: dict[str, list[int]] = {}
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Assert)
                        and isinstance(sub.test, ast.Compare)
                        and isinstance(sub.test.left, (ast.Attribute, ast.Name))
                    ):
                        for op, comp in zip(sub.test.ops, sub.test.comparators, strict=False):
                            if (
                                isinstance(op, (ast.Is, ast.IsNot))
                                and isinstance(comp, ast.Constant)
                                and isinstance(comp.value, bool)
                            ):
                                expr_str = ast.unparse(sub.test.left)
                                seen_asserts.setdefault(expr_str, []).append(sub.lineno)

                for expr_str, lines in seen_asserts.items():
                    if len(lines) > 1:
                        findings.append(
                            f"[{py_path.relative_to(ROOT)}:{lines[1]}] Unnecessary Comparison Invariant: "
                            f"Repeated boolean identity assertion on '{expr_str}' in '{node.name}' (lines {lines}). "
                            f"Use 'assert {expr_str}' / 'assert not {expr_str}' to avoid Pyrefly "
                            f"unnecessary-comparison narrowing errors."
                        )

    return findings


@app.command()
def main(
    strict: Annotated[
        bool, typer.Option("--strict/--no-strict", help="Exit with code 1 if integrity violations are found.")
    ] = True,
) -> None:
    """Execute automated test integrity audit across Python test suites."""
    all_findings = audit_python_tests() + audit_test_only_hooks() + audit_boolean_identity_assertions()
    print("\n--- TEST INTEGRITY AUDIT RESULTS ---")
    if not all_findings:
        print("✅ 100% of test functions have substantive assertions and pass Rule 17/18 integrity gates.")
        print("✅ Zero test-only hooks or dead backdoors detected in production code (Rule 5 compliant).")
        print("✅ Zero repeated boolean identity assertions in test suites (Pyrefly unnecessary-comparison compliant).")
        return

    print(f"❌ Found {len(all_findings)} test integrity violation(s):")
    for f in all_findings:
        print(f"  {f}\n::error::{f}")
    if strict:
        sys.exit(1)


if __name__ == "__main__":
    app()
