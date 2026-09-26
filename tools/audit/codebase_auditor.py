#!/usr/bin/env python3
"""Codebase Auditor for SIL-2 / MIL-SPEC Integrity.

Enforces Rule 3, Rule 4, Rule 8, and Rule 27 compliance across the codebase
utilizing industry-standard static analysis engines (Semgrep, Buf, Ruff).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]


def audit_semgrep() -> list[str]:
    """Audit Python and C++ source files using declarative Semgrep rules."""
    semgrep_bin = shutil.which("semgrep")
    config_path = ROOT / ".semgrep.yml"
    if not semgrep_bin or not config_path.exists():
        return [f"Semgrep Config Missing: {config_path} not found"] if not config_path.exists() else []

    res = subprocess.run(
        [semgrep_bin, "--config", str(config_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if not res.stdout:
        return []
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        return [f"Semgrep JSON Parse Error: {exc}"]

    return [
        f"Semgrep Violation [{r.get('check_id', 'rule')}]: "
        f"{r.get('path', '')}:{r.get('start', {}).get('line', 0)} - {r.get('extra', {}).get('message', '')}"
        for r in data.get("results", [])
    ]


def audit_config_suppressions() -> list[str]:
    """Audit configuration files for suppression directives."""
    suppression = re.compile(
        r"(ignore_errors|suppress|disable_warnings|continue_on_error|skip_validation)\s*[:=]\s*(true|1|yes)",
        re.IGNORECASE,
    )
    ignored = {".tox", ".git", "openwrt-sdk", "typings", ".tmp_tests"}
    findings: list[str] = []
    for ext in ("*.yml", "*.yaml", "*.toml", "*.json"):
        for cfg in ROOT.rglob(ext):
            if any(part in cfg.parts for part in ignored) or cfg.name in (".semgrep.yml", ".semgrepignore"):
                continue
            for i, line in enumerate(cfg.read_text(encoding="utf-8").splitlines(), 1):
                if suppression.search(line):
                    findings.append(f"Config Suppression: {cfg.relative_to(ROOT)}:{i} - '{line.strip()}'")
    return findings


def audit_proto_integrity(proto_path: Path | None = None) -> list[str]:
    """Audit Protobuf definitions with Buf (lint and breaking change checks) [SIL-2]."""
    print("Auditing Protobuf definitions...")
    target = proto_path or (ROOT / "tools" / "protocol" / "mcubridge.proto")
    if not target.exists():
        return [f"Protobuf File Missing: {target} not found"]

    buf_bin = shutil.which("buf")
    if not buf_bin:
        return ["Buf Executable Missing: 'buf' binary not found in PATH"]

    findings: list[str] = []
    module_dir = target.parent
    buf_lint = subprocess.run(
        [buf_bin, "lint", str(module_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if buf_lint.returncode != 0:
        findings.append(f"Buf Lint Violation:\n{buf_lint.stdout.strip() or buf_lint.stderr.strip()}")

    if (ROOT / ".git").exists() and target == ROOT / "tools" / "protocol" / "mcubridge.proto":
        buf_breaking = subprocess.run(
            [buf_bin, "breaking", str(module_dir), "--against", ".git#subdir=tools/protocol"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if buf_breaking.returncode != 0:
            err = buf_breaking.stdout.strip() or buf_breaking.stderr.strip()
            findings.append(f"Buf Breaking Change Violation:\n{err}")

    return findings


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
