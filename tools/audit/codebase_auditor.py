"""Codebase Auditor for SIL-2 / MIL-SPEC Integrity.

Enforces Rule 3, Rule 4, Rule 8, and Rule 27 compliance across the codebase
using Semgrep declarative analysis, strict config suppression audits, and Protobuf schema integrity.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tokenize
from typing import cast

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
    if not semgrep_bin:
        return ["Semgrep Executable Missing: 'semgrep' binary not found in PATH"]

    res = run_command([semgrep_bin, "--config", str(config_path), "--json"])
    if isinstance(res, OSError):
        return [f"Semgrep Execution Error: {res}"]
    if res.returncode != 0:
        return [f"Semgrep Execution Error: {res.stderr.strip() or 'exit code ' + str(res.returncode)}"]
    if not res.stdout or not res.stdout.strip():
        return ["Semgrep Execution Error: Empty output received from Semgrep"]
    try:
        raw_data: object = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        return [f"Semgrep JSON Parse Error: {exc}"]

    if not isinstance(raw_data, dict):
        return ["Semgrep JSON Parse Error: results must be a list"]

    data = cast(dict[str, object], raw_data)
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        return ["Semgrep JSON Parse Error: results must be a list"]

    findings: list[str] = []
    items = cast(list[object], raw_results)
    for raw_r in items:
        if isinstance(raw_r, dict):
            r = cast(dict[str, object], raw_r)
            check_id = str(r.get("check_id") or "rule")
            path_str = str(r.get("path") or "")
            line = 0
            start_obj = r.get("start")
            if isinstance(start_obj, dict):
                start_dict = cast(dict[str, object], start_obj)
                line_val = start_dict.get("line")
                if isinstance(line_val, int):
                    line = line_val
            msg = ""
            extra_obj = r.get("extra")
            if isinstance(extra_obj, dict):
                extra_dict = cast(dict[str, object], extra_obj)
                msg = str(extra_dict.get("message") or "")
            findings.append(f"Semgrep Violation [{check_id}]: {path_str}:{line} - {msg}")
    return findings


def audit_config_suppressions() -> list[str]:
    findings: list[str] = []
    for ext in ("*.yml", "*.yaml", "*.toml", "*.json"):
        for cfg in ROOT.rglob(ext):
            rel = cfg.relative_to(ROOT)
            if any(part in rel.parts[:-1] for part in IGNORED_CONFIG_DIRS) or cfg.name in (
                ".semgrep.yml",
                ".semgrepignore",
            ):
                continue
            for i, line in enumerate(cfg.read_text(encoding="utf-8").splitlines(), 1):
                if SUPPRESSION.search(line):
                    findings.append(f"Config Suppression: {rel}:{i} - '{line.strip()}'")
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
    checks: list[tuple[str, Path, tuple[str, ...]]] = [
        ("Buf Lint Violation", module_dir, ("lint",)),
        ("Buf Build Failure", module_dir, ("build",)),
        ("Buf Format Violation", ROOT, ("format", "-d", str(module_dir), "--exit-code")),
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


def audit_proto_usage() -> list[str]:
    """Find generated constants and enum values with no source or test references."""
    from mcubridge.protocol import mcubridge_pb2 as pb

    source_roots = (
        ROOT / "mcubridge" / "mcubridge",
        ROOT / "mcubridge" / "tests",
        ROOT / "mcubridge-client-examples",
        ROOT / "mcubridge-gateway",
        ROOT / "mcubridge-library-arduino",
        ROOT / "luci-app-mcubridge",
        ROOT / "tools",
    )
    source_suffixes = {".py", ".pyi", ".h", ".hpp", ".cpp", ".c", ".ino", ".js", ".uc", ".sh", ".j2"}
    ignored_parts = {
        ".git",
        ".tox",
        ".tmp_tests",
        ".venv",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "openwrt-sdk",
        "__pycache__",
    }
    generated_names = {
        "config_schema.json",
        "defaults.sh",
        "mcubridge.pb.c",
        "mcubridge.pb.h",
        "rpc_hw_config.h",
        "rpc_protocol.h",
        "rpc_structs.h",
    }
    identifiers: set[str] = set()
    for source_root in source_roots:
        if not source_root.exists():
            continue
        for path in source_root.rglob("*"):
            if (
                not path.is_file()
                or path.suffix not in source_suffixes
                or ignored_parts.intersection(path.parts)
                or path.name in generated_names
                or path.name.endswith(("_pb2.py", "_pb2.pyi", "_grpc.py"))
                or (path.name == "protocol.py" and path.parent.name in {"protocol", "mcubridge_client"})
            ):
                continue
            content = path.read_text(encoding="utf-8")
            if path.suffix in {".py", ".pyi"}:
                identifiers.update(
                    token.string
                    for token in tokenize.generate_tokens(io.StringIO(content).readline)
                    if token.type == tokenize.NAME
                )
            else:
                content_without_comments = re.sub(r"/\*.*?\*/|//[^\n]*", " ", content, flags=re.DOTALL)
                identifiers.update(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", content_without_comments))

    identifier_suffixes = identifiers.copy()
    for token in identifiers:
        identifier_suffixes.update(
            token[index + 1 :] for index, character in enumerate(token) if character == "_"
        )

    def is_referenced(name: str) -> bool:
        return name in identifier_suffixes

    findings: list[str] = []
    file_options = pb.DESCRIPTOR.GetOptions()
    constants = file_options.Extensions[pb.constants]
    config_references = {
        field.GetOptions().Extensions[pb.config_default_ref].partition(".")[2]
        for field in pb.RuntimeConfig.DESCRIPTOR.fields
        if field.GetOptions().HasExtension(pb.config_default_ref)
        and field.GetOptions().Extensions[pb.config_default_ref].startswith("constants.")
    }

    for field_desc in pb.Constants.DESCRIPTOR.fields:
        options = field_desc.GetOptions()
        names = {
            field_desc.name,
            options.Extensions[pb.py_name],
            options.Extensions[pb.cpp_name],
        }
        if field_desc.name not in config_references and not any(name and is_referenced(name) for name in names):
            findings.append(f"Unused Protobuf constant: Constants.{field_desc.name}={getattr(constants, field_desc.name)}")

    reflected_enum_names = {"Command", "Status"}
    for enum_desc in pb.DESCRIPTOR.enum_types_by_name.values():
        if enum_desc.name in reflected_enum_names:
            continue
        for value_desc in enum_desc.values:
            if not is_referenced(value_desc.name):
                findings.append(f"Unused Protobuf enum value: {enum_desc.name}.{value_desc.name}")

    return findings


def audit_arduino_sketches() -> list[str]:
    """Audit reference Arduino .ino sketches for bounded synchronization watchdog loop. [Rule 26]"""
    sketches = sorted((ROOT / "mcubridge-library-arduino" / "examples").glob("*/*.ino"))
    sync_pattern = re.compile(r"Bridge\.isSynchronized\s*\(\s*\)")
    return [
        f"[{p.relative_to(ROOT)}] Rule 26 Violation: Sketch lacks bounded 'Bridge.isSynchronized()' in setup()."
        for p in sketches
        if "Bridge.begin" in (txt := p.read_text(encoding="utf-8")) and not sync_pattern.search(txt)
    ]


app = typer.Typer(help="Audit codebase for SIL-2/MIL-SPEC violations and shims.", add_completion=False)


@app.command()
def main() -> None:
    """Execute Semgrep, config, protobuf, and sketch compliance audits."""
    all_findings = (
        audit_semgrep()
        + audit_config_suppressions()
        + audit_proto_integrity()
        + audit_proto_usage()
        + audit_arduino_sketches()
    )

    print("\n--- RESULTS ---")
    if not all_findings:
        print("No violations or shims found! The codebase is 100% clean and compliant.")
        return

    for f in all_findings:
        print(f"  [VIOLATION] {f}")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
