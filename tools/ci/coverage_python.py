#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Python test runner and branch coverage validator."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Annotated

import pytest
import typer

app = typer.Typer(
    help="Run pytest with strict branch coverage gates and export reports.",
    add_completion=False,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_COVERAGE_ROOT = ROOT_DIR / "coverage" / "python"
DEFAULT_TARGETS = ("mcubridge/tests", "mcubridge-client-examples/tests", "mcubridge-gateway/tests")


@app.command()
def main(
    output_root: Annotated[
        Path, typer.Option("--output-root", help="Output directory for coverage reports")
    ] = DEFAULT_COVERAGE_ROOT,
    html: Annotated[bool, typer.Option("--html/--no-html", help="Enable/disable HTML report")] = True,
    emit_json: Annotated[bool, typer.Option("--json/--no-json", help="Emit coverage.json report")] = True,
    min_coverage: Annotated[float, typer.Option("--min-coverage", help="Minimum total coverage percentage")] = 95.0,
    min_branch: Annotated[float, typer.Option("--min-branch", help="Minimum pure branch coverage percentage")] = 95.0,
    pytest_args: Annotated[
        list[str] | None, typer.Argument(help="Optional extra arguments or test paths passed to pytest")
    ] = None,
) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    coverage_file = output_root / ".coverage"
    os.environ["COVERAGE_FILE"] = str(coverage_file)

    # Set PYTHONPATH
    pythonpath_dirs = [
        str(ROOT_DIR / "typings"),
        str(ROOT_DIR / "typings" / "stubs"),
        str(ROOT_DIR),
        str(ROOT_DIR / "mcubridge"),
        str(ROOT_DIR / "mcubridge-client-examples"),
        str(ROOT_DIR / "mcubridge-gateway"),
    ]
    cur_pythonpath = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = ":".join(pythonpath_dirs + ([cur_pythonpath] if cur_pythonpath else []))

    targets = list(pytest_args) if pytest_args else list(DEFAULT_TARGETS)

    args = [
        "-vv",
        "-p",
        "pytest_asyncio",
        "-p",
        "no:platformdirs",
        "--timeout=300",
        "--timeout-method=thread",
        f"--cov={ROOT_DIR / 'mcubridge' / 'mcubridge'}",
        f"--cov={ROOT_DIR / 'mcubridge-client-examples' / 'mcubridge_client'}",
        f"--cov={ROOT_DIR / 'mcubridge-gateway' / 'gateway.py'}",
        "--cov-branch",
        f"--cov-fail-under={min_coverage:.1f}",
        f"--cov-report=xml:{output_root / 'coverage.xml'}",
        "--cov-report=term-missing",
    ]

    if html:
        args.append(f"--cov-report=html:{output_root / 'html'}")

    has_n = any(arg == "-n" or arg.startswith("-n=") for arg in targets)
    if not has_n and importlib.util.find_spec("xdist") is not None:
        args.extend(["-n", "5"])

    args.extend(targets)

    print(f"[coverage_python] Running pytest with {len(args)} arguments...")
    exit_code = pytest.main(args)
    if exit_code != 0:
        print(f"[coverage_python] pytest failed with exit code {exit_code}", file=sys.stderr)
        sys.exit(exit_code)

    # Generate JSON report via direct coverage library API [Rule 37]
    import coverage

    json_path = output_root / "coverage.json"
    cov = coverage.Coverage(data_file=str(coverage_file))
    cov.load()
    cov.json_report(outfile=str(json_path))

    if not json_path.exists():
        print(f"[coverage_python] ERROR: Coverage JSON report was not generated at {json_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    totals = data.get("totals", {})
    branch_pct = float(totals.get("percent_branches_covered", 0.0))
    num_branches = totals.get("num_branches", 0)
    covered_branches = totals.get("covered_branches", 0)

    summary_str = f"{branch_pct:.2f}% ({covered_branches}/{num_branches})"
    if branch_pct < min_branch:
        print(
            f"FAIL Required pure branch test coverage of {min_branch}% not reached. "
            f"Pure branch coverage: {summary_str}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Required pure branch test coverage of {min_branch}% reached. Pure branch coverage: {summary_str}")

    if not emit_json and json_path.exists():
        json_path.unlink()


if __name__ == "__main__":
    app()
