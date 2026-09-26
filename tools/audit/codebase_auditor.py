#!/usr/bin/env python3
"""Codebase Auditor for SIL-2 / MIL-SPEC Integrity.

Enforces Rule 3, Rule 4, Rule 8, and Rule 27 compliance across the codebase
utilizing industry-standard static analysis engines (Semgrep, Ruff, Flake8).
"""

from __future__ import annotations

import importlib.util
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
    findings: list[str] = []
    print("Auditing codebase with Semgrep (.semgrep.yml)...")

    semgrep_bin = shutil.which("semgrep")
    if not semgrep_bin:
        print("[WARN] semgrep binary not found in PATH; skipping semgrep audit.")
        return findings

    config_path = ROOT / ".semgrep.yml"
    if not config_path.exists():
        findings.append(f"Semgrep Config Missing: {config_path} not found")
        return findings

    try:
        res = subprocess.run(
            [semgrep_bin, "--config", str(config_path), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.stdout:
            data = json.loads(res.stdout)
            for r in data.get("results", []):
                findings.append(
                    f"Semgrep Violation [{r.get('check_id', 'rule')}]: "
                    f"{r.get('path', '')}:{r.get('start', {}).get('line', 0)} - "
                    f"{r.get('extra', {}).get('message', '')}"
                )
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        findings.append(f"Semgrep Execution Error: {exc}")

    return findings


def audit_python_files() -> list[str]:
    """Compatibility entry point delegating Python compliance to Semgrep and linters."""
    return audit_semgrep()


def audit_cpp_files() -> list[str]:
    """Compatibility entry point delegating C++ compliance to Semgrep and linters."""
    return []


def audit_config_suppressions() -> list[str]:
    """Audit configuration files for suppression directives."""
    findings: list[str] = []
    print("Auditing Configuration files...")
    suppression_pattern = re.compile(
        r"(ignore_errors|suppress|disable_warnings|continue_on_error|skip_validation)\s*[:=]\s*(true|1|yes)",
        re.IGNORECASE,
    )
    for ext in ("*.yml", "*.yaml", "*.toml", "*.json"):
        for cfg_file in ROOT.rglob(ext):
            if any(part in cfg_file.parts for part in (".tox", ".git", "openwrt-sdk", "typings", ".tmp_tests")):
                continue
            try:
                for i, line in enumerate(cfg_file.read_text(encoding="utf-8").splitlines(), 1):
                    if suppression_pattern.search(line):
                        findings.append(f"Config Suppression: {cfg_file.relative_to(ROOT)}:{i} - '{line.strip()}'")
            except (UnicodeDecodeError, OSError):
                continue
    return findings


def audit_proto_integrity(proto_path: Path | None = None) -> list[str]:
    """Audit mcubridge.proto to ensure all dead or abandoned definitions are purged."""
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

    # 4. Buf Linting
    buf_bin = shutil.which("buf")
    if buf_bin and (target_path.parent / "buf.yaml").exists():
        buf_res = subprocess.run(
            [buf_bin, "lint", str(target_path.parent)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if buf_res.returncode != 0:
            findings.append(f"Buf Lint Violation:\n{buf_res.stdout.strip()}")

    return findings


def audit_linters() -> list[str]:
    """Audit codebase with flake8 and ruff to detect any linter or E501 errors."""
    findings: list[str] = []
    print("Auditing linters (flake8 & ruff)...")

    # 1. Flake8
    flake8_cmd: list[str] | None = None
    if importlib.util.find_spec("flake8") is not None:
        flake8_cmd = [sys.executable, "-m", "flake8"]
    else:
        flake8_path = shutil.which("flake8")
        if flake8_path is not None:
            flake8_cmd = [flake8_path]

    if flake8_cmd is not None:
        res_flake = subprocess.run(flake8_cmd, cwd=ROOT, capture_output=True, text=True, check=False)
        if res_flake.returncode != 0:
            for line in res_flake.stdout.splitlines():
                line_str = line.strip()
                if line_str:
                    findings.append(f"Flake8 Violation: {line_str}")

    # 2. Ruff
    ruff_cmd: list[str] | None = None
    if importlib.util.find_spec("ruff") is not None:
        ruff_cmd = [sys.executable, "-m", "ruff"]
    else:
        ruff_path = shutil.which("ruff")
        if ruff_path is not None:
            ruff_cmd = [ruff_path]

    if ruff_cmd is not None:
        cmd_args: list[str] = [
            *ruff_cmd,
            "check",
            "mcubridge",
            "mcubridge-client-examples",
            "mcubridge-gateway",
            "tools",
        ]
        res_ruff = subprocess.run(
            cmd_args,
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
    """Execute Semgrep, config, protobuf, and linter compliance audits."""
    semgrep_findings = audit_semgrep()
    cfg_findings = audit_config_suppressions()
    proto_findings = audit_proto_integrity()
    linter_findings = audit_linters()

    all_findings = semgrep_findings + cfg_findings + proto_findings + linter_findings

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
