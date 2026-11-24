"""Codebase Auditor for SIL-2 / MIL-SPEC Integrity.

Enforces Rule 3, Rule 4, Rule 8, and Rule 27 compliance across the codebase
using Semgrep declarative analysis, strict config suppression audits, and Protobuf schema integrity.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Iterator, Sequence
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

from google.protobuf.descriptor import Descriptor, EnumDescriptor, FileDescriptor

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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | OSError:
    try:
        return subprocess.run(
            command,
            cwd=cwd or ROOT,
            env=env,
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


def _inspect_ast_node(node: ast.AST, rel_path: Path) -> list[str]:
    if isinstance(node, (ast.Try, ast.TryStar)):
        violations = [
            f"Rule 4 AST Violation: {rel_path}:{stmt.lineno} - Catching exceptions with 'pass' is strictly prohibited."
            for handler in node.handlers
            for stmt in handler.body
            if isinstance(stmt, ast.Pass)
        ]
        for handler in node.handlers:
            is_broad = handler.type is None or (
                isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException")
            )
            for stmt in handler.body:
                if isinstance(stmt, (ast.Break, ast.Continue)) and is_broad:
                    name = (
                        handler.type.id
                        if isinstance(handler.type, ast.Name)
                        else ("bare except" if handler.type is None else "broad except")
                    )
                    action = stmt.__class__.__name__.lower()
                    violations.append(
                        f"Rule 4 AST Violation: {rel_path}:{stmt.lineno} - "
                        f"Catching broad '{name}' with '{action}' is strictly prohibited. "
                        "Catch specific exceptions or log explicitly."
                    )
        return violations
    if isinstance(node, ast.match_case):
        return [
            f"Rule 4 AST Violation: {rel_path}:{stmt.lineno} - Match case with 'pass' is strictly prohibited."
            for stmt in node.body
            if isinstance(stmt, ast.Pass)
        ]
    if (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
    ):
        return [f"Rule 4 AST Violation: {rel_path}:{node.lineno} - TYPE_CHECKING dual-standard is strictly prohibited."]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [
            f"Rule 4 AST Violation: {rel_path}:{node.lineno} - TYPE_CHECKING dual-standard is strictly prohibited."
            for alias in node.names
            if alias.name == "TYPE_CHECKING" or alias.asname == "TYPE_CHECKING"
        ]
    if isinstance(node, ast.Call):
        return [
            f"Rule 4 AST Violation: {rel_path}:{node.lineno} - "
            f"errors={kw.value.value!r} silent error masking is strictly prohibited."
            for kw in node.keywords
            if (
                kw.arg == "errors"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value in ("ignore", "replace", "backslashreplace")
            )
        ]
    return []


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
        "third_party",
    }
    for path in ROOT.rglob("*.py"):
        try:
            rel_path = path.relative_to(ROOT)
        except ValueError:
            rel_path = path
        if ignored_parts.intersection(rel_path.parts[:-1]):
            continue
        try:
            raw_bytes = path.read_bytes()
            tree = ast.parse(raw_bytes, filename=str(path))
            tokens = tokenize.tokenize(io.BytesIO(raw_bytes).readline)
            findings.extend(
                f"Rule 4 Violation: {rel_path}:{tok.start[0]} - "
                f"{'# ' + 'noqa: E402'} comment suppression is strictly prohibited."
                for tok in tokens
                if tok.type == tokenize.COMMENT and re.search(r"noqa\b.*?\bE402\b", tok.string, re.IGNORECASE)
            )

        except (SyntaxError, UnicodeDecodeError, OSError, tokenize.TokenError) as exc:
            findings.append(f"AST Parse Error: {rel_path} - {exc}")
            continue
        for node in ast.walk(tree):
            findings.extend(_inspect_ast_node(node, rel_path))

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

    env = os.environ.copy()
    if "SEMGREP_CORE_EXTRA" not in env:
        env["SEMGREP_CORE_EXTRA"] = "-use_parmap"

    res = run_command([semgrep_bin, "--config", str(config_path), "--json"], env=env)
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
    return findings + _semgrep_coverage_findings(data.get("errors"))


# Semgrep skips constructs it cannot parse (PROGMEM placement, `#if` inside an
# argument list, function-like macros) and reports them as PartialParsing.
# Rules never see those lines, so only short skipped regions are tolerated.
SEMGREP_MAX_SKIPPED_LINES = 3


def _semgrep_error_type(error: dict[str, object]) -> str:
    raw_type = error.get("type")
    if isinstance(raw_type, list):
        type_parts = cast(list[object], raw_type)
        return str(type_parts[0]) if type_parts else "unknown"
    return str(raw_type) if raw_type else "unknown"


def _semgrep_coverage_findings(raw_errors: object) -> list[str]:
    """Turn Semgrep errors into findings so unanalysed code never passes silently."""
    if not isinstance(raw_errors, list):
        return []
    findings: list[str] = []
    for raw_error in cast(list[object], raw_errors):
        if not isinstance(raw_error, dict):
            continue
        error = cast(dict[str, object], raw_error)
        error_type = _semgrep_error_type(error)
        path_str = str(error.get("path") or "")
        if error_type != "PartialParsing":
            message = str(error.get("message") or "").strip().splitlines()
            detail = message[0] if message else "no message"
            findings.append(f"Semgrep Coverage Error [{error_type}]: {path_str} - {detail}")
            continue
        raw_spans = error.get("spans")
        spans = cast(list[object], raw_spans) if isinstance(raw_spans, list) else []
        for raw_span in spans:
            if not isinstance(raw_span, dict):
                continue
            span = cast(dict[str, dict[str, int]], raw_span)
            start = span.get("start", {}).get("line", 0)
            end = span.get("end", {}).get("line", start)
            if end - start + 1 > SEMGREP_MAX_SKIPPED_LINES:
                findings.append(
                    f"Semgrep Coverage Error [PartialParsing]: {path_str}:{start}-{end} - "
                    f"{end - start + 1} lines unparsed (max {SEMGREP_MAX_SKIPPED_LINES})"
                )
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


_PROTO_USAGE_SUFFIXES = {".py", ".pyi", ".h", ".hpp", ".cpp", ".cc", ".c", ".ino", ".js", ".uc", ".sh", ".j2"}
_PROTO_USAGE_IGNORED = {
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
    "third_party",
    "Unity",
}
_PROTO_GENERATED_NAMES = {
    "config_schema.json",
    "defaults.sh",
    "config_schema.pb.c",
    "config_schema.pb.h",
    "mcu_wire.pb.c",
    "mcu_wire.pb.h",
    "mcu_wire.rpc.pb.h",
    "rpc_hw_config.h",
    "rpc_protocol.h",
}
_PROTO_GENERATED_SUFFIXES = ("_pb2.py", "_pb2.pyi", "_grpc.py", ".pb.h", ".pb.c", ".pwpb.h", ".rpc.pb.h")
_PROD_PROTO_REL_ROOTS: tuple[tuple[str, ...], ...] = (
    ("mcubridge", "mcubridge"),
    ("mcubridge", "scripts"),
    ("mcubridge-client-examples", "mcubridge_client"),
    ("mcubridge-client-examples", "examples"),
    ("mcubridge-library-arduino",),
    ("mcubridge-gateway-cpp",),
    ("luci-app-mcubridge",),
    ("tools", "protocol"),
)
_ALL_PROTO_REL_ROOTS: tuple[tuple[str, ...], ...] = (
    *_PROD_PROTO_REL_ROOTS,
    ("mcubridge", "tests"),
    ("mcubridge-client-examples", "tests"),
    ("tools",),
)


def _is_scannable_proto_source(path: Path, *, root: Path | None = None, include_tests: bool = False) -> bool:
    if not path.is_file() or path.suffix not in _PROTO_USAGE_SUFFIXES:
        return False
    base_root = root or ROOT
    try:
        rel_path = path.relative_to(base_root)
    except ValueError:
        rel_path = path
    if _PROTO_USAGE_IGNORED.intersection(rel_path.parts) or path.name in _PROTO_GENERATED_NAMES:
        return False
    if not include_tests and ("tests" in rel_path.parts[:-1] or rel_path.parts[:2] == ("tools", "audit")):
        return False
    if path.name.endswith(_PROTO_GENERATED_SUFFIXES):
        return False
    return not (path.name == "protocol.py" and path.parent.name in {"protocol", "mcubridge_client"})


def _extract_file_identifiers(path: Path) -> set[str]:
    content = path.read_text(encoding="utf-8")
    if path.suffix in {".py", ".pyi"}:
        return {
            token.string
            for token in tokenize.generate_tokens(io.StringIO(content).readline)
            if token.type == tokenize.NAME
        }
    content_without_comments = re.sub(r"/\*.*?\*/|//[^\n]*", " ", content, flags=re.DOTALL)
    return set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", content_without_comments))


def _collect_proto_identifiers(*, root: Path | None = None, include_tests: bool = False) -> set[str]:
    base_root = root or ROOT
    rel_roots = _ALL_PROTO_REL_ROOTS if include_tests else _PROD_PROTO_REL_ROOTS
    identifiers: set[str] = set()
    seen_paths: set[Path] = set()
    for rel_parts in rel_roots:
        source_root = base_root.joinpath(*rel_parts)
        if not source_root.exists():
            continue
        for path in source_root.rglob("*"):
            if path in seen_paths or not _is_scannable_proto_source(path, root=base_root, include_tests=include_tests):
                continue
            seen_paths.add(path)
            identifiers.update(_extract_file_identifiers(path))
    return identifiers


def _collect_identifier_suffixes(root: Path | None = None) -> set[str]:
    identifiers = _collect_proto_identifiers(root=root, include_tests=True)
    identifier_suffixes = identifiers.copy()
    for token in identifiers:
        suffix = token
        while "_" in suffix:
            suffix = suffix.partition("_")[2]
            identifier_suffixes.add(suffix)
    return identifier_suffixes


def collect_production_proto_identifiers(root: Path | None = None) -> set[str]:
    """Collect identifiers from production, C++, LuCI, and codegen sources (excluding tests and generated files)."""
    identifiers = _collect_proto_identifiers(root=root, include_tests=False)
    return identifiers | {token.removeprefix("rpc_pb_") for token in identifiers}


def _is_map_entry_message(message_desc: Descriptor) -> bool:
    return message_desc.GetOptions().map_entry


def _iter_descriptor_messages(descriptor: FileDescriptor) -> Iterator[Descriptor]:
    stack: list[Descriptor] = list(descriptor.message_types_by_name.values())
    while stack:
        message_desc = stack.pop()
        if _is_map_entry_message(message_desc):
            continue
        yield message_desc
        stack.extend(message_desc.nested_types)


def _iter_descriptor_enums(descriptor: FileDescriptor) -> Iterator[EnumDescriptor]:
    yield from descriptor.enum_types_by_name.values()
    for message_desc in _iter_descriptor_messages(descriptor):
        yield from message_desc.enum_types


def _collect_extension_type_names(descriptors: Sequence[FileDescriptor]) -> set[str]:
    """Collect full names of message/enum types used exclusively as google.protobuf.*Options extensions."""
    ext_types: set[str] = set()
    pending_messages: deque[Descriptor] = deque()
    for descriptor in descriptors:
        for ext in descriptor.extensions_by_name.values():
            if ext.message_type is not None:
                pending_messages.append(ext.message_type)
            if ext.enum_type is not None:
                ext_types.add(ext.enum_type.full_name)
    while pending_messages:
        message_desc = pending_messages.popleft()
        if message_desc.full_name in ext_types:
            continue
        ext_types.add(message_desc.full_name)
        for field in message_desc.fields:
            if field.message_type is not None and field.message_type.full_name not in ext_types:
                pending_messages.append(field.message_type)
            if field.enum_type is not None:
                ext_types.add(field.enum_type.full_name)
    return ext_types


def _relative_proto_name(descriptor: FileDescriptor, full_name: str) -> str:
    prefix = f"{descriptor.package}."
    return full_name.removeprefix(prefix) if descriptor.package and full_name.startswith(prefix) else full_name


def audit_proto_descriptors_reachability(
    descriptors: Sequence[FileDescriptor],
    prod_identifiers: set[str],
) -> list[str]:
    """Verify every non-extension message and enum is reachable from production/codegen callers."""
    ext_type_names = _collect_extension_type_names(descriptors)
    direct_tokens = prod_identifiers | {token.removeprefix("rpc_pb_") for token in prod_identifiers}

    messages_by_full_name: dict[str, tuple[FileDescriptor, Descriptor]] = {}
    enums_by_full_name: dict[str, tuple[FileDescriptor, EnumDescriptor]] = {}
    for file_desc in descriptors:
        for message_desc in _iter_descriptor_messages(file_desc):
            if message_desc.full_name not in ext_type_names:
                messages_by_full_name[message_desc.full_name] = (file_desc, message_desc)
        for enum_desc in _iter_descriptor_enums(file_desc):
            if enum_desc.full_name not in ext_type_names:
                enums_by_full_name[enum_desc.full_name] = (file_desc, enum_desc)

    reachable: set[str] = set()
    queue: deque[Descriptor] = deque()

    for full_name, (_, message_desc) in messages_by_full_name.items():
        if message_desc.name in direct_tokens:
            reachable.add(full_name)
            queue.append(message_desc)

    for full_name, (_, enum_desc) in enums_by_full_name.items():
        if enum_desc.name in direct_tokens or any(
            not val.name.endswith("_UNSPECIFIED")
            and (val.name in direct_tokens or f"{enum_desc.name}_{val.name}" in direct_tokens)
            for val in enum_desc.values
        ):
            reachable.add(full_name)

    while queue:
        current = queue.popleft()
        for field in current.fields:
            target_msg = field.message_type
            if target_msg is not None and _is_map_entry_message(target_msg):
                value_field = target_msg.fields_by_name.get("value")
                if value_field is not None:
                    if (
                        value_field.message_type is not None
                        and value_field.message_type.full_name in messages_by_full_name
                        and value_field.message_type.full_name not in reachable
                    ):
                        reachable.add(value_field.message_type.full_name)
                        queue.append(value_field.message_type)
                    if value_field.enum_type is not None and value_field.enum_type.full_name in enums_by_full_name:
                        reachable.add(value_field.enum_type.full_name)
                continue
            if (
                target_msg is not None
                and target_msg.full_name in messages_by_full_name
                and target_msg.full_name not in reachable
            ):
                reachable.add(target_msg.full_name)
                queue.append(target_msg)
            if field.enum_type is not None and field.enum_type.full_name in enums_by_full_name:
                reachable.add(field.enum_type.full_name)

    findings: list[str] = []
    for file_desc in descriptors:
        unreachable_msgs = sorted(
            _relative_proto_name(file_desc, msg.full_name)
            for msg in _iter_descriptor_messages(file_desc)
            if msg.full_name in messages_by_full_name and msg.full_name not in reachable
        )
        findings.extend(f"Unused Protobuf message: {file_desc.name}:{name}" for name in unreachable_msgs)
        unreachable_enums = sorted(
            _relative_proto_name(file_desc, enum_desc.full_name)
            for enum_desc in _iter_descriptor_enums(file_desc)
            if enum_desc.full_name in enums_by_full_name and enum_desc.full_name not in reachable
        )
        findings.extend(f"Unused Protobuf enum: {file_desc.name}:{name}" for name in unreachable_enums)
    return findings


def audit_proto_usage(root: Path | None = None) -> list[str]:
    """Find unused constants, enum values, and unreachable Protobuf messages/enums across all schemas."""
    from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2 as pb, mcu_wire_pb2
    from mcubridge.protocol.gateway import gateway_control_pb2

    identifier_suffixes = _collect_identifier_suffixes(root)
    findings: list[str] = []
    constants = pb.DESCRIPTOR.GetOptions().Extensions[pb.constants]
    config_references = {
        field.GetOptions().Extensions[pb.config_default_ref].partition(".")[2]
        for field in pb.RuntimeConfig.DESCRIPTOR.fields
        if field.GetOptions().HasExtension(pb.config_default_ref)
        and field.GetOptions().Extensions[pb.config_default_ref].startswith("constants.")
    }

    for field_desc in pb.Constants.DESCRIPTOR.fields:
        options = field_desc.GetOptions()
        names = {field_desc.name, options.Extensions[pb.py_name], options.Extensions[pb.cpp_name]}
        if field_desc.name not in config_references and not any(name and name in identifier_suffixes for name in names):
            findings.append(
                f"Unused Protobuf constant: Constants.{field_desc.name}={getattr(constants, field_desc.name)}"
            )

    for enum_desc in pb.DESCRIPTOR.enum_types_by_name.values():
        findings.extend(
            f"Unused Protobuf enum value: {enum_desc.name}.{value_desc.name}"
            for value_desc in enum_desc.values
            if not value_desc.name.endswith("_UNSPECIFIED") and value_desc.name not in identifier_suffixes
        )

    descriptors: tuple[FileDescriptor, ...] = (
        pb.DESCRIPTOR,
        mcu_wire_pb2.DESCRIPTOR,
        cloud_gateway_pb2.DESCRIPTOR,
        gateway_control_pb2.DESCRIPTOR,
    )
    prod_identifiers = collect_production_proto_identifiers(root)
    findings.extend(audit_proto_descriptors_reachability(descriptors, prod_identifiers))
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
