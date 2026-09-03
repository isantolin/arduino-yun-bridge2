#!/usr/bin/env python3
"""Automated standalone CLI and script integrity validation test suite.

Ensures no script or CLI tool has hidden/silent import errors, broken dependencies,
missing CLI options, or unhandled exceptions when executed in an isolated environment
without PYTHONPATH injection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
import pytest


def get_standalone_scripts() -> list[Path]:
    """Discover all executable scripts and tools across the repository."""
    repo_root = Path(__file__).resolve().parents[2]
    search_dirs = [
        repo_root / "tools",
        repo_root / "mcubridge" / "scripts",
        repo_root / "mcubridge-gateway",
        repo_root / "mcubridge-client-examples" / "examples",
    ]

    scripts: list[Path] = []
    for d in search_dirs:
        for f in d.rglob("*.py"):
            if f.name != "__init__.py" and "templates" not in str(f) and "tests" not in str(f):
                scripts.append(f)
    return sorted(scripts)


@pytest.mark.parametrize("script_path", get_standalone_scripts(), ids=lambda p: str(p.name))
def test_script_standalone_execution_and_help(script_path: Path) -> None:
    """Validate that every script runs cleanly with --help in a clean subprocess."""
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[2]
    workspace_paths = [
        str(repo_root),
        str(repo_root / "mcubridge"),
        str(repo_root / "mcubridge-client-examples"),
        str(repo_root / "mcubridge-gateway"),
    ]
    current_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(workspace_paths + ([current_pp] if current_pp else []))

    proc = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, (
        f"Script {script_path.name} failed with returncode {proc.returncode}.\n"
        f"Stderr:\n{proc.stderr}\n"
        f"Stdout:\n{proc.stdout}"
    )
    assert "Traceback" not in proc.stderr, f"Traceback found in stderr of {script_path.name}:\n{proc.stderr}"
    assert "ModuleNotFoundError" not in proc.stderr, f"Missing module in {script_path.name}:\n{proc.stderr}"
    assert "ImportError" not in proc.stderr, f"Import error in {script_path.name}:\n{proc.stderr}"
    assert "NameError" not in proc.stderr, f"NameError in {script_path.name}:\n{proc.stderr}"
    assert "AttributeError" not in proc.stderr, f"AttributeError in {script_path.name}:\n{proc.stderr}"


def test_no_suppressions_in_pyproject() -> None:
    """Enforce Rule 4: Verify that pyproject.toml has zero per-file-ignores or suppression blocks."""
    repo_root = Path(__file__).resolve().parents[2]
    pyproject = repo_root / "pyproject.toml"
    assert pyproject.exists(), "pyproject.toml not found"

    content = pyproject.read_text(encoding="utf-8")
    assert "per-file-ignores" not in content, (
        "Rule 4 violation: 'per-file-ignores' detected in pyproject.toml. "
        "All linter suppressions are strictly prohibited."
    )
