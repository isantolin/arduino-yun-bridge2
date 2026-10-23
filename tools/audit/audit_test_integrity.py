#!/usr/bin/env python3
"""Automated Test Integrity Auditor (SIL-2 / MIL-SPEC).

Enforces Rule 11, Rule 17, and Rule 18 compliance:
1. 100% of Python test functions must assert concrete state or emitted frames.
C++ test assertion (sil2-ban-cpp-test-without-assert), tautology, and discarded call
checks are delegated to Semgrep (.semgrep.yml) and Ruff (B018, PT).
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
from typing import Annotated

import typer

ROOT = Path(__file__).resolve().parents[2]

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
    "hexdump_processor",
    "coerce_path",
    "coerce_commands",
    "runtime_config_factory",
    "load_runtime_config_from_json",
    "replace_cloud_publish",
    "extract_nonce_counter",
    "secure_zero",
    "get_subscriptions",
    "check_rate_limit",
    "set_fsm_state",
    "synchronize_attempt",
    "fetch_capabilities_with_delay",
    "fetch_capabilities",
    "wait_for_link_sync_confirmation",
    "flush_cloud_spool",
    "record_cloud_drop",
    "spool_cloud_message_locked",
    "write_with_quota",
    "reject_cloud",
    "run_cloud",
    "connect_cloud_session",
    "send_cloud_event",
    "register_methods",
    "reset_handlers",
    "build_serial_pipeline_snapshot",
    "write_status_file",
    "safe_after_configure",
    "resolve_serial_url",
    "switch_local_baudrate",
    "toggle_dtr",
    "read_loop",
    "process_packet",
    "correlate_frame",
    "check_baudrate_fallback",
    "is_healthy",
    "is_draining",
    "is_cloud_connected",
    "_build_bridge_snapshot_message",
    "generate_spec_file",
    "create_source_tarball",
    "ingest_telemetry",
    "post_line",
    "format_line_protocol",
    "auth_interceptor",
    "handle_command_response",
    "unsupported_mcu_request",
    "lookup_topic",
    "request_mcu_version",
    "normalize_config_dict",
    "allows_topic",
    "vacuum_lmdb_env",
    "connect_and_run",
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


def audit_test_only_hooks() -> list[str]:
    """Scan production code for methods and functions called exclusively from tests (Rule 5 violation)."""
    findings: list[str] = []
    print("Auditing test-only hooks and dead backdoors (Rule 5)...")

    # 1. Python Analysis
    py_prod_files = [
        f
        for f in (ROOT / "mcubridge" / "mcubridge").rglob("*.py")
        if not f.name.endswith(("_pb2.py", "_grpc.py", "_pb2.pyi"))
    ]
    py_test_files = list((ROOT / "mcubridge" / "tests").rglob("*.py"))

    py_prod_text = "\n".join(f.read_text(encoding="utf-8") for f in py_prod_files)
    py_test_text = "\n".join(f.read_text(encoding="utf-8") for f in py_test_files)

    for pfile in py_prod_files:
        try:
            file_content = pfile.read_text(encoding="utf-8")
            tree = ast.parse(file_content, filename=str(pfile))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
                if (
                    name.startswith(("__", "ubus_handle_", "on_mcu_", "handle_", "_on_mcu_", "on_enter_", "on_exit_"))
                    or name[0].isupper()
                    or name in IGNORED_IDENTIFIERS
                    or len(name) < 4
                    or name in ALLOWLIST_PY_HELPERS
                ):
                    continue

                pattern = rf"\b{re.escape(name)}\b"
                prod_count = len(re.findall(pattern, py_prod_text))
                file_count = len(re.findall(pattern, file_content))
                test_count = len(re.findall(pattern, py_test_text))

                if prod_count == file_count and file_count <= 2 and test_count > 0:
                    findings.append(
                        f"[{pfile.relative_to(ROOT)}:{node.lineno}] Rule 5 Violation: "
                        f"Production function/method '{name}' is only invoked from test suites."
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
