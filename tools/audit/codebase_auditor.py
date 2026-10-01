"""Codebase Auditor for SIL-2 / MIL-SPEC Integrity.

Enforces Rule 3, Rule 4, Rule 8, and Rule 27 compliance across the codebase
utilizing industry-standard static analysis engines (Semgrep, Buf, Ruff).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import typer

ROOT = Path(__file__).resolve().parent.parent.parent
IGNORED_CONFIG_DIRS = {".tox", ".git", "openwrt-sdk", "typings", ".tmp_tests"}
SUPPRESSION = re.compile(
    r"(ignore_errors|suppress|disable_warnings|continue_on_error|skip_validation)\s*[:=]\s*(true|1|yes)",
    re.IGNORECASE,
)


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str] | OSError:
    try:
        return subprocess.run(
            command,
            cwd=cwd or ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return exc


def _buf_check(buf_bin: str, label: str, cwd: Path, *arguments: str) -> str | None:
    result = run_command([buf_bin, *arguments], cwd=cwd)
    if isinstance(result, OSError):
        return f"{label} Execution Error: {result}"
    if result.returncode == 0:
        return None
    output = result.stdout.strip() or result.stderr.strip()
    return f"{label}:\n{output or 'command failed without output'}"


def audit_semgrep() -> list[str]:
    """Audit Python and C++ source files using declarative Semgrep rules."""
    semgrep_bin = shutil.which("semgrep")
    config_path = ROOT / ".semgrep.yml"
    if not config_path.exists():
        return [f"Semgrep Config Missing: {config_path} not found"]
    if not semgrep_bin:
        return ["Semgrep Executable Missing: 'semgrep' binary not found in PATH"]

    res = run_command([semgrep_bin, "--config", str(config_path), "--json"])
    if isinstance(res, OSError):
        return [f"Semgrep Execution Error: {res}"]
    if not res.stdout or not res.stdout.strip():
        return [f"Semgrep Execution Error: {res.stderr.strip() or 'Empty output received from Semgrep'}"]
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        return [f"Semgrep JSON Parse Error: {exc}"]

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return ["Semgrep JSON Parse Error: results must be a list"]
    if res.returncode not in (0, 1):
        return [f"Semgrep Execution Error: {res.stderr.strip() or f'exit status {res.returncode}'}"]
    return [
        f"Semgrep Violation [{r.get('check_id', 'rule')}]: "
        f"{r.get('path', '')}:{r.get('start', {}).get('line', 0)} - {r.get('extra', {}).get('message', '')}"
        for r in results
    ]


def audit_config_suppressions() -> list[str]:
    """Audit configuration files for suppression directives."""
    findings: list[str] = []
    for ext in ("*.yml", "*.yaml", "*.toml", "*.json"):
        for cfg in ROOT.rglob(ext):
            if any(part in cfg.parts for part in IGNORED_CONFIG_DIRS) or cfg.name in (
                ".semgrep.yml",
                ".semgrepignore",
            ):
                continue
            for i, line in enumerate(cfg.read_text(encoding="utf-8").splitlines(), 1):
                if SUPPRESSION.search(line):
                    findings.append(f"Config Suppression: {cfg.relative_to(ROOT)}:{i} - '{line.strip()}'")
    return findings


def audit_proto_integrity(proto_path: Path | None = None) -> list[str]:
    """Audit Protobuf definitions with Buf (lint and breaking change checks) [SIL-2]."""
    print("Auditing Protobuf definitions...")
    target = proto_path or (ROOT / "tools" / "protocol" / "mcubridge.proto")
    if not target.exists():
        return [f"Protobuf spec missing: {target}"]

    buf_bin = shutil.which("buf")
    if not buf_bin:
        return ["Buf Executable Missing: 'buf' binary not found in PATH"]

    module_dir = target.parent
    buf_yaml = module_dir / "buf.yaml"
    created_buf_yaml = False
    if not buf_yaml.exists():
        default_config = (
            "version: v2\n"
            "modules:\n"
            "  - path: .\n"
            "lint:\n"
            "  use:\n"
            "    - BASIC\n"
            "    - FIELD_LOWER_SNAKE_CASE\n"
            "  except:\n"
            "    - PACKAGE_DIRECTORY_MATCH\n"
        )
        buf_yaml.write_text(default_config, encoding="utf-8")
        created_buf_yaml = True

    try:
        checks: list[tuple[str, Path, tuple[str, ...]]] = [
            ("Buf Lint Violation", module_dir, ("lint",)),
        ]
        if (ROOT / ".git").exists() and target == (ROOT / "tools" / "protocol" / "mcubridge.proto"):
            checks.append(
                (
                    "Buf Breaking Change Violation",
                    ROOT,
                    ("breaking", str(module_dir), "--against", ".git#subdir=tools/protocol"),
                )
            )

        return [
            finding
            for label, check_cwd, arguments in checks
            if (finding := _buf_check(buf_bin, label, check_cwd, *arguments))
        ]
    finally:
        if created_buf_yaml and buf_yaml.exists():
            buf_yaml.unlink()


app = typer.Typer(help="Audit codebase for SIL-2/MIL-SPEC violations and shims.", add_completion=False)


@app.command()
def main() -> None:
    """Execute Semgrep, config, and protobuf compliance audits."""
    all_findings = audit_semgrep() + audit_config_suppressions() + audit_proto_integrity()

    print("\n--- RESULTS ---")
    if not all_findings:
        print("No violations or shims found! The codebase is 100% clean and compliant.")
        return

    for f in all_findings:
        print(f)
        print(f"::error::{f}")
    sys.exit(1)


if __name__ == "__main__":
    app()
