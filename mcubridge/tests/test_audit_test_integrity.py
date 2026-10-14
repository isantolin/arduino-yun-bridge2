"""Unit tests for tools/audit/audit_test_integrity.py.

Validates Rule 17/18 substantive test assertions and Rule 5 zero test-only hooks detection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tools.audit.audit_test_integrity import (
    audit_cpp_tests,
    audit_python_tests,
    audit_test_only_hooks,
    main,
)


def test_audit_test_only_hooks_clean_codebase() -> None:
    """Verify that current clean codebase has zero test-only hooks or dead backdoors."""
    findings = audit_test_only_hooks()
    assert findings == [], f"Unexpected test-only hooks detected:\n{findings}"


def test_audit_test_only_hooks_positive_detection(tmp_path: Path) -> None:
    """Positive test: verify that a method in prod called only from tests is flagged."""
    fake_prod = tmp_path / "mcubridge" / "mcubridge" / "dummy_service.py"
    fake_test = tmp_path / "mcubridge" / "tests" / "test_dummy.py"
    fake_prod.parent.mkdir(parents=True, exist_ok=True)
    fake_test.parent.mkdir(parents=True, exist_ok=True)

    fake_prod.write_text(
        "class DummyService:\n    def internal_test_only_hook(self):\n        return 42\n",
        encoding="utf-8",
    )
    fake_test.write_text(
        "def test_dummy():\n    svc = DummyService()\n    assert svc.internal_test_only_hook() == 42\n",
        encoding="utf-8",
    )

    with patch("tools.audit.audit_test_integrity.ROOT", tmp_path):
        findings = audit_test_only_hooks()

    assert any("internal_test_only_hook" in f for f in findings)
    assert any("Rule 5 Violation" in f for f in findings)


def test_audit_python_tests_detects_empty_assertions(tmp_path: Path) -> None:
    """Verify that Python test functions without assertions are flagged."""
    fake_test = tmp_path / "mcubridge" / "tests" / "test_empty.py"
    fake_test.parent.mkdir(parents=True, exist_ok=True)

    fake_test.write_text(
        "def test_function_without_assertions():\n    x = 1 + 1\n",
        encoding="utf-8",
    )

    with patch("tools.audit.audit_test_integrity.ROOT", tmp_path):
        findings = audit_python_tests()

    assert any("Rule 17/18 Violation" in f for f in findings)
    assert any("test_function_without_assertions" in f for f in findings)


def test_audit_cpp_tests_detects_empty_assertions(tmp_path: Path) -> None:
    """Verify that C++ test functions without TEST_ASSERT macros are flagged."""
    fake_cpp = tmp_path / "mcubridge-library-arduino" / "tests" / "test_empty.cpp"
    fake_cpp.parent.mkdir(parents=True, exist_ok=True)

    fake_cpp.write_text(
        "void test_unasserted_case() {\n  int a = 10;\n}\n",
        encoding="utf-8",
    )

    with patch("tools.audit.audit_test_integrity.ROOT", tmp_path):
        findings = audit_cpp_tests()

    assert any("Rule 17/18 Violation" in f for f in findings)
    assert any("test_unasserted_case" in f for f in findings)


def test_main_cli_success() -> None:
    """Verify main() CLI executes cleanly without raising SystemExit on compliant repo."""
    res = main(strict=True)
    assert res is None


def test_main_cli_failure_on_violations(tmp_path: Path) -> None:
    """Verify main() CLI exits with code 1 when violations are detected."""
    fake_test = tmp_path / "mcubridge" / "tests" / "test_empty.py"
    fake_test.parent.mkdir(parents=True, exist_ok=True)
    fake_test.write_text("def test_empty(): pass\n", encoding="utf-8")
    with patch("tools.audit.audit_test_integrity.ROOT", tmp_path), pytest.raises(SystemExit) as exc:
        main(strict=True)
    assert exc.value.code == 1
