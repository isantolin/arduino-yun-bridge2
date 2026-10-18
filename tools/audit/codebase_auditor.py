"""Codebase Auditor for SIL-2 / MIL-SPEC Integrity.

Enforces Rule 3, Rule 4, Rule 8, and Rule 27 compliance across the codebase
using Semgrep declarative analysis, strict config suppression audits, and Protobuf schema integrity.
"""

from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tokenize
import tomllib
from typing import cast

import sys
import typer
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "mcubridge") not in sys.path:
    sys.path.insert(0, str(ROOT / "mcubridge"))
IGNORED_CONFIG_DIRS = {".tox", ".git", "openwrt-sdk", "typings", ".tmp_tests", "Unity"}
FORBIDDEN_SUPPRESSIONS = frozenset(
    {
        "ignore_errors",
        "suppress",
        "disable_warnings",
        "continue_on_error",
        "skip_validation",
    }
)


def _find_dict_suppressions(data: object, prefix: str = "") -> list[str]:
    violations: list[str] = []
    if isinstance(data, dict):
        d = cast(dict[object, object], data)
        for k, v in d.items():
            key_name = str(k).lower()
            current_path = f"{prefix}.{k}" if prefix else str(k)
            if key_name in FORBIDDEN_SUPPRESSIONS and (
                v is True or (isinstance(v, (str, int)) and str(v).lower() in ("true", "1", "yes"))
            ):
                violations.append(f"{current_path} = {v!r}")
            violations.extend(_find_dict_suppressions(v, current_path))
    elif isinstance(data, list):
        items = cast(list[object], data)
        for idx, item in enumerate(items):
            violations.extend(_find_dict_suppressions(item, f"{prefix}[{idx}]"))
    return violations


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


def audit_ast_integrity() -> list[str]:
    """Audit Python AST for Rule 4 violations: except-pass, case-pass, and TYPE_CHECKING."""
    print("Auditing Python AST integrity (Rule 4)...")
    findings: list[str] = []
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
        "typings",
        "Unity",
    }
    for path in ROOT.rglob("*.py"):
        try:
            rel_path = path.relative_to(ROOT)
        except ValueError:
            rel_path = path
        if ignored_parts.intersection(rel_path.parts[:-1]):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            findings.append(f"AST Parse Error: {rel_path} - {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Try, ast.TryStar)):
                for handler in node.handlers:
                    findings.extend(
                        f"Rule 4 AST Violation: {rel_path}:{stmt.lineno} - "
                        f"Catching exceptions with 'pass' is strictly prohibited."
                        for stmt in handler.body
                        if isinstance(stmt, ast.Pass)
                    )
            elif isinstance(node, ast.match_case):
                findings.extend(
                    f"Rule 4 AST Violation: {rel_path}:{stmt.lineno} - "
                    f"Match case with 'pass' is strictly prohibited."
                    for stmt in node.body
                    if isinstance(stmt, ast.Pass)
                )
            elif (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
                isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
            ):
                findings.append(
                    f"Rule 4 AST Violation: {rel_path}:{node.lineno} - "
                    f"TYPE_CHECKING dual-standard is strictly prohibited."
                )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                findings.extend(
                    f"Rule 4 AST Violation: {rel_path}:{node.lineno} - "
                    f"TYPE_CHECKING dual-standard is strictly prohibited."
                    for alias in node.names
                    if alias.name == "TYPE_CHECKING" or alias.asname == "TYPE_CHECKING"
                )

    return findings


def audit_semgrep() -> list[str]:
    """Audit Python and C++ source files using declarative Semgrep rules."""
    semgrep_bin = shutil.which("semgrep")
    if not semgrep_bin:
        for candidate in (
            ROOT / ".tox" / "semgrep" / "bin" / "semgrep",
            Path.home() / ".local" / "bin" / "semgrep",
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                semgrep_bin = str(candidate)
                break
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
    """Audit config files (.yml, .yaml, .toml, .json) for suppression directives [Rule 4]."""
    findings: list[str] = []
    for ext in ("*.yml", "*.yaml", "*.toml", "*.json"):
        for cfg in ROOT.rglob(ext):
            rel = cfg.relative_to(ROOT)
            if any(part in rel.parts[:-1] for part in IGNORED_CONFIG_DIRS) or cfg.name in (
                ".semgrep.yml",
                ".semgrepignore",
            ):
                continue
            try:
                content = cfg.read_text(encoding="utf-8")
                parsed_docs: list[object] = []
                if cfg.suffix == ".json":
                    parsed_docs.append(json.loads(content))
                elif cfg.suffix == ".toml":
                    parsed_docs.append(tomllib.loads(content))
                elif cfg.suffix in (".yaml", ".yml"):
                    parsed_docs.extend(yaml.safe_load_all(content))

                for doc in parsed_docs:
                    findings.extend(f"Config Suppression: {rel} - '{v}'" for v in _find_dict_suppressions(doc))
            except (OSError, ValueError, yaml.YAMLError) as exc:
                findings.append(f"Config Parse Error: {rel} - {exc}")
    return findings


def audit_proto_integrity(proto_path: Path | None = None) -> list[str]:
    """Audit Protobuf definitions with Buf (lint and breaking change checks) [SIL-2]."""
    print("Auditing Protobuf definitions...")
    target = proto_path or (ROOT / "tools" / "protocol" / "cloud_gateway.proto")
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
    if (ROOT / ".git").exists() and target == (ROOT / "tools" / "protocol" / "cloud_gateway.proto"):
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
        ROOT / "mcubridge" / "scripts",
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
        "config_schema.pb.c",
        "config_schema.pb.h",
        "mcu_wire.pb.c",
        "mcu_wire.pb.h",
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
        suffix = token
        while "_" in suffix:
            suffix = suffix.partition("_")[2]
            identifier_suffixes.add(suffix)

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
            findings.append(
                f"Unused Protobuf constant: Constants.{field_desc.name}={getattr(constants, field_desc.name)}"
            )

    reflected_enum_names = {"Command", "Status"}
    for enum_desc in pb.DESCRIPTOR.enum_types_by_name.values():
        if enum_desc.name in reflected_enum_names:
            continue
        findings.extend(
            f"Unused Protobuf enum value: {enum_desc.name}.{value_desc.name}"
            for value_desc in enum_desc.values
            if not is_referenced(value_desc.name)
        )

    extension_message_types = {
        ext.message_type.name for ext in pb.DESCRIPTOR.extensions_by_name.values() if ext.message_type
    }
    for ext in pb.DESCRIPTOR.extensions_by_name.values():
        if ext.message_type:
            for field in ext.message_type.fields:
                if field.message_type:
                    extension_message_types.add(field.message_type.name)
    for message_desc in pb.DESCRIPTOR.message_types_by_name.values():
        if message_desc.name in extension_message_types:
            continue
        if not is_referenced(message_desc.name):
            findings.append(f"Unused Protobuf message: {message_desc.name}")

    return findings


app = typer.Typer(help="Audit codebase for SIL-2/MIL-SPEC violations and shims.", add_completion=False)


@app.command()
def main() -> None:
    """Execute Semgrep, config, and protobuf compliance audits."""
    all_findings = (
        audit_ast_integrity()
        + audit_semgrep()
        + audit_config_suppressions()
        + audit_proto_integrity()
        + audit_proto_usage()
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
