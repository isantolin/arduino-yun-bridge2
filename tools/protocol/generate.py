#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Jinja2: Declarative templates for C++ and Python outputs.

Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib
import importlib.util
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, cast

import black
import typer
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import MessageToDict
from jinja2 import Environment, FileSystemLoader
from packaging.version import Version

# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY VALIDATION (CRITICAL)
# ═════════════════════════════════════════════════════════════════════════════
REQUIRED_DEPS = ["jinja2", "google.protobuf", "nanopb", "mypy_protobuf", "grpclib", "black", "typer", "packaging"]

MISSING_DEPS: list[str] = [dep for dep in REQUIRED_DEPS if importlib.util.find_spec(dep.split(".")[0]) is None]

HAS_BUF: bool = shutil.which("buf") is not None

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if MISSING_DEPS or not HAS_BUF:
    sys.stderr.write("\n" + "!" * 80 + "\n")
    sys.stderr.write("ERROR: Missing dependencies required for protocol generation:\n")
    for dep in MISSING_DEPS:
        sys.stderr.write(f"  - {dep} (Python)\n")
    if not HAS_BUF:
        sys.stderr.write("  - buf (Buf CLI not found in PATH)\n")
    sys.stderr.write("\nTo fix this, run:\n")
    if MISSING_DEPS:
        sys.stderr.write(f"  pip install {' '.join(MISSING_DEPS)}\n")
    if not HAS_BUF:
        sys.stderr.write("  npm install -g @bufbuild/buf\n")
    sys.stderr.write("!" * 80 + "\n\n")
    sys.exit(1)
# ═════════════════════════════════════════════════════════════════════════════


# Mappings and helper functions for reflective protocol constant generation.


def _resolve_nanopb_protoc_dir() -> Path | None:
    """Locate the directory containing nanopb's bundled protoc binary."""
    spec = importlib.util.find_spec("nanopb")
    if spec and spec.origin:
        candidate = Path(spec.origin).parent / "generator"
        if (candidate / "protoc").exists():
            return candidate
    return None


def run_buf_generate(proto_dir: Path) -> None:
    """Run ``buf generate`` declaratively (replaces manual protoc/nanopb subprocess calls).

    Buf orchestrates all protoc plugins defined in ``buf.gen.yaml``:
    python pb2, mypy stubs, grpclib, and nanopb C.
    """
    import os

    env = os.environ.copy()
    py_bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = f"{py_bin_dir}:{env.get('PATH', '')}"
    # Ensure nanopb's bundled protoc is reachable for buf's protoc_builtin plugins
    nanopb_dir = _resolve_nanopb_protoc_dir()
    if nanopb_dir:
        env["PATH"] = f"{nanopb_dir}:{env['PATH']}"

    try:
        subprocess.run(
            ["buf", "generate"],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(proto_dir),
            env=env,
        )
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"Error: buf generate failed:\n{e.stderr}\n")
        sys.exit(1)


@dataclass(frozen=True, slots=True)
class ConfigFieldDef:
    name: str
    field_type: str
    default_value: Any
    raw_default: str
    description: str
    is_volatile: bool
    min_val: float | None
    max_val: float | None
    uci_option: str | None
    config_default_ref: str | None
    client_constant: bool


_message_to_dict: Final = functools.partial(
    MessageToDict, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
)


def _snake_case(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def _format_cpp_digit(v: object) -> str:
    """Format integers >= 10'000 with C++14 digit separators. [SIL-2]"""
    return f"{v:_}".replace("_", "'") if isinstance(v, int) and abs(v) >= 10_000 else str(v)




_FIELD_CONVERTERS: dict[int, tuple[str, Callable[[Any], Any]]] = {
    FieldDescriptor.TYPE_STRING: ("str", lambda v: str(v) if v is not None else ""),
    FieldDescriptor.TYPE_BYTES: ("bytes", lambda v: v.encode("utf-8") if isinstance(v, str) else (v or b"")),
    FieldDescriptor.TYPE_BOOL: ("bool", lambda v: str(v).lower() in ("true", "1", "yes") if v is not None else False),
    FieldDescriptor.TYPE_FLOAT: ("float", lambda v: float(v) if v is not None else 0.0),
    FieldDescriptor.TYPE_DOUBLE: ("float", lambda v: float(v) if v is not None else 0.0),
    FieldDescriptor.TYPE_INT32: ("int", lambda v: int(v) if v is not None else 0),
    FieldDescriptor.TYPE_INT64: ("int", lambda v: int(v) if v is not None else 0),
    FieldDescriptor.TYPE_UINT32: ("int", lambda v: int(v) if v is not None else 0),
    FieldDescriptor.TYPE_UINT64: ("int", lambda v: int(v) if v is not None else 0),
}


def _resolve_field_default(
    field_desc: Any, opts: Any, pb_module: Any, default_sources: dict[str, Any]
) -> tuple[Any, str | None]:
    has_literal_default = opts.HasExtension(pb_module.config_default)
    has_default_ref = opts.HasExtension(pb_module.config_default_ref)
    if has_literal_default and has_default_ref:
        raise ValueError(f"RuntimeConfig field '{field_desc.name}' has both default value and default reference")
    if not has_default_ref:
        return (opts.Extensions[pb_module.config_default] if has_literal_default else None), None
    reference = opts.Extensions[pb_module.config_default_ref]
    source_name, separator, field_name = reference.partition(".")
    source = default_sources.get(source_name)
    if not separator or not field_name or source is None:
        raise ValueError(f"Invalid RuntimeConfig default reference '{reference}'")
    if field_name not in source.DESCRIPTOR.fields_by_name:
        raise ValueError(f"Unknown RuntimeConfig default reference '{reference}'")
    return getattr(source, field_name), reference


def _opt_ext(opts: Any, ext: Any, default: Any = None) -> Any:
    return opts.Extensions[ext] if opts.HasExtension(ext) else default


def _load_runtime_config_fields(file_desc: Any, pb_module: Any) -> list[ConfigFieldDef]:
    all_descs = [file_desc, *file_desc.dependencies]
    runtime_config_desc = next(
        (
            dep.message_types_by_name["RuntimeConfig"]
            for dep in all_descs
            if "RuntimeConfig" in dep.message_types_by_name
        ),
        None,
    )
    if not runtime_config_desc:
        return []

    default_sources: dict[str, Any] = {}
    for dep in all_descs:
        opts = dep.GetOptions()
        if opts.HasExtension(pb_module.constants):
            default_sources["constants"] = opts.Extensions[pb_module.constants]
        if opts.HasExtension(pb_module.cloud_defaults):
            default_sources["cloud_defaults"] = opts.Extensions[pb_module.cloud_defaults]

    runtime_config_fields: list[ConfigFieldDef] = []
    for field_desc in runtime_config_desc.fields:
        opts = field_desc.GetOptions()
        cfg_default, config_default_ref = _resolve_field_default(field_desc, opts, pb_module, default_sources)
        if field_desc.is_repeated:
            py_type, typed_val = "list", []
        elif field_desc.type in _FIELD_CONVERTERS:
            py_type, converter = _FIELD_CONVERTERS[field_desc.type]
            typed_val = converter(cfg_default)
        else:
            py_type, typed_val = "message", None

        runtime_config_fields.append(
            ConfigFieldDef(
                name=field_desc.name,
                field_type=py_type,
                default_value=typed_val,
                raw_default=str(cfg_default) if cfg_default is not None else "",
                description=_opt_ext(opts, pb_module.config_desc, ""),
                is_volatile=_opt_ext(opts, pb_module.config_volatile, False),
                min_val=_opt_ext(opts, pb_module.config_min),
                max_val=_opt_ext(opts, pb_module.config_max),
                uci_option=_opt_ext(opts, pb_module.uci_option),
                config_default_ref=config_default_ref,
                client_constant=_opt_ext(opts, pb_module.client_constant, False),
            )
        )
    return runtime_config_fields


TEMPLATE_DIR = Path(__file__).parent / "templates"
VERSION_PATH = REPO_ROOT / "VERSION"


def _extract_constants_and_handshake(
    pb_module: Any,
    opts: Any,
    v_major: int,
    v_minor: int,
    v_patch: int,
) -> dict[str, Any]:
    c_opt = opts.Extensions[pb_module.constants]
    hw_opt = opts.Extensions[pb_module.hardware]
    hs_opt = opts.Extensions[pb_module.handshake]

    cpp_consts: list[dict[str, Any]] = []
    py_consts: list[dict[str, Any]] = []
    client_consts: list[dict[str, Any]] = []

    for pb_obj in (c_opt, hw_opt):
        for f in pb_obj.DESCRIPTOR.fields:
            fo = f.GetOptions()
            val = getattr(pb_obj, f.name)
            cpp_n, cpp_t = fo.Extensions[pb_module.cpp_name], fo.Extensions[pb_module.cpp_type]
            if cpp_n:
                cpp_consts.append({"name": cpp_n, "type": cpp_t, "value": val})
            py_n, py_t = fo.Extensions[pb_module.py_name], fo.Extensions[pb_module.py_type]
            if py_n:
                fmt_val = (
                    f"bytes([ {val} ])"
                    if py_n == "FRAME_DELIMITER"
                    else f'b"{val}"' if py_t == "bytes" else f'"{val}"' if py_t == "str" else val
                )
                c_def = {"name": py_n, "type": py_t, "value": fmt_val}
                py_consts.append(c_def)
                if pb_obj is c_opt and fo.Extensions[pb_module.client_constant]:
                    client_consts.append(c_def)

    for n, v in [
        ("FIRMWARE_VERSION_MAJOR", v_major),
        ("FIRMWARE_VERSION_MINOR", v_minor),
        ("FIRMWARE_VERSION_PATCH", v_patch),
    ]:
        cpp_consts.append({"name": n, "type": "uint8_t", "value": v})
        py_consts.append({"name": n, "type": "int", "value": v})
        client_consts.append({"name": n, "type": "int", "value": v})

    cloud_suffixes = _message_to_dict(opts.Extensions[pb_module.cloud_suffixes])
    for f in pb_module.CloudSuffixes.DESCRIPTOR.fields:
        cloud_suffixes.setdefault(f.name, f.name)

    for n, v in cloud_suffixes.items():
        cpp_consts.append({"name": f"RPC_CLOUD_SUFFIX_{n.upper()}", "type": "const char*", "value": f'"{v}"'})
        py_consts.append({"name": f"CLOUD_SUFFIX_{n.upper()}", "type": "str", "value": f'"{v}"'})

    hs_cpp: list[dict[str, Any]] = []
    hs_py: list[dict[str, Any]] = []
    for f in hs_opt.DESCRIPTOR.fields:
        fo = f.GetOptions()
        val = getattr(hs_opt, f.name)
        cn, ct = fo.Extensions[pb_module.cpp_name], fo.Extensions[pb_module.cpp_type]
        if cn:
            hs_cpp.append({"name": cn, "type": ct, "value": val})
        pn, pt = fo.Extensions[pb_module.py_name], fo.Extensions[pb_module.py_type]
        if pn:
            fmt_v = (
                f"bytes([ {val} ])"
                if pn == "FRAME_DELIMITER"
                else f'b"{val}"' if pt == "bytes" else f'"{val}"' if pt == "str" else val
            )
            hs_py.append({"name": pn, "type": pt, "value": fmt_v})

    salt, info_a, info_s = hs_opt.hkdf_salt, hs_opt.hkdf_info_auth, hs_opt.hkdf_info_session
    handshake_dict = {
        "hkdf_salt": salt,
        "hkdf_salt_bytes": ", ".join(f"0x{ord(c):02X}" for c in salt),
        "hkdf_salt_len": len(salt),
        "hkdf_info_auth": info_a,
        "hkdf_info_auth_bytes": ", ".join(f"0x{ord(c):02X}" for c in info_a),
        "hkdf_info_auth_len": len(info_a),
        "hkdf_info_session": info_s,
        "hkdf_info_session_bytes": ", ".join(f"0x{ord(c):02X}" for c in info_s),
        "hkdf_info_session_len": len(info_s),
    }

    return {
        "constants": cpp_consts,
        "python_constants": py_consts,
        "client_constants": client_consts,
        "v_major": v_major,
        "v_minor": v_minor,
        "v_patch": v_patch,
        "cloud_suffixes": cloud_suffixes,
        "handshake_constants": hs_cpp,
        "python_handshake_constants": hs_py,
        "handshake": handshake_dict,
    }


def _extract_commands_and_statuses(
    pb_module: Any, cmd_enum: Any, status_enum: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    commands: list[dict[str, Any]] = []
    for value in cmd_enum.values:
        if value.name == "CMD_UNSPECIFIED":
            continue
        command_options = value.GetOptions().Extensions[pb_module.cmd_opts]
        command = _message_to_dict(command_options)
        command["name"] = value.name
        command["value"] = value.number
        command["expects_direct_response"] = bool(command_options.response_command)
        if command_options.HasField("mcu_dispatch"):
            command["mcu_dispatch"] = _message_to_dict(command_options.mcu_dispatch)
        commands.append(command)

    request_by_response = {
        command["response_command"]: command["name"]
        for command in commands
        if command.get("response_command")
    }
    for command in commands:
        if not command.get("request_command") and command["name"] in request_by_response:
            command["request_command"] = request_by_response[command["name"]]

    statuses: list[dict[str, Any]] = []
    for value in status_enum.values:
        if value.name == "STATUS_UNSPECIFIED":
            continue
        status_options = value.GetOptions().Extensions[pb_module.status_opts]
        status = {
            "name": value.name,
            "value": value.number,
            "description": status_options.description,
        }
        if status_options.HasField("mcu_dispatch"):
            status["mcu_dispatch"] = _message_to_dict(status_options.mcu_dispatch)
        statuses.append(status)
    return commands, statuses


def _build_mcu_dispatch_entries(
    commands: list[dict[str, Any]], statuses: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the static C++ dispatch table from Protobuf command/status options."""
    entries: list[dict[str, Any]] = []
    adapter_prefix = "MCU_DISPATCH_ADAPTER_"

    for kind, items in (("command", commands), ("status", statuses)):
        for item in items:
            raw_dispatch = item.get("mcu_dispatch")
            if not raw_dispatch:
                continue
            if not isinstance(raw_dispatch, dict):
                raise ValueError(f"Invalid MCU dispatch metadata for {item['name']}")
            dispatch = dict(raw_dispatch)
            adapter = dispatch.get("adapter", "")
            if not isinstance(adapter, str) or not adapter.startswith(adapter_prefix):
                raise ValueError(f"Missing or invalid MCU dispatch adapter for {item['name']}")
            dispatch["adapter"] = adapter.removeprefix(adapter_prefix).lower()

            expression = _format_mcu_dispatch_expression(dispatch, item["name"])
            feature_macro = dispatch.get("feature_macro") or ""
            if feature_macro and (
                not isinstance(feature_macro, str)
                or re.fullmatch(r"BRIDGE_ENABLE_[A-Z0-9_]+", feature_macro) is None
            ):
                raise ValueError(f"Invalid MCU dispatch feature macro for {item['name']}")
            key = (
                f"rpc::StatusCode::STATUS_{item['name']}"
                if kind == "status"
                else f"rpc::CommandId::{item['name']}"
            )
            entries.append(
                {
                    "name": item["name"],
                    "value": item["value"],
                    "key": key,
                    "expression": expression,
                    "feature_macro": feature_macro,
                }
            )

    entries.sort(key=lambda entry: entry["value"])
    numeric_ids = [entry["value"] for entry in entries]
    if len(numeric_ids) != len(set(numeric_ids)):
        raise ValueError("MCU dispatch table contains duplicate command/status IDs")
    return entries


def _format_mcu_dispatch_expression(dispatch: dict[str, Any], name: str) -> str:
    """Render a fixed-allocation MCU dispatch adapter instantiation."""
    adapters = {
        "member_no_payload",
        "member_with_msg",
        "member_with_ctx_msg",
        "static_with_msg",
        "target_with_msg",
        "target_action",
    }
    adapter = dispatch.get("adapter", "")
    if adapter not in adapters:
        raise ValueError(f"Unknown MCU dispatch adapter {adapter!r} for {name}")

    handler = dispatch.get("handler", "")
    handler_class = dispatch.get("handler_class") or "BridgeClass"
    if not isinstance(handler, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", handler) is None:
        raise ValueError(f"Invalid MCU dispatch handler for {name}: {handler!r}")
    if not isinstance(handler_class, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", handler_class) is None:
        raise ValueError(f"Invalid MCU dispatch handler class for {name}: {handler_class!r}")

    handler_args = dispatch.get("handler_template_args", "")
    handler_ptr = f"&{handler_class}::{handler}"
    if handler_args:
        if not isinstance(handler_args, str):
            raise ValueError(f"Invalid handler template arguments for {name}")
        handler_ptr += f"<{handler_args}>"

    payload_type = dispatch.get("payload_type", "")
    needs_payload = adapter in {
        "member_with_msg",
        "member_with_ctx_msg",
        "static_with_msg",
        "target_with_msg",
    }
    if needs_payload and (
        not isinstance(payload_type, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", payload_type) is None
    ):
        raise ValueError(f"Missing or invalid MCU dispatch payload type for {name}")

    target_instance = dispatch.get("target_instance", "")
    if adapter in {"target_with_msg", "target_action"} and (
        not isinstance(target_instance, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target_instance) is None
    ):
        raise ValueError(f"Missing or invalid MCU dispatch target instance for {name}")

    flags = {
        "needs_ack": dispatch.get("needs_ack", True),
        "retransmit_on_dup": dispatch.get("retransmit_on_dup", False),
        "check_dup": dispatch.get("check_dup", True),
    }
    if not all(isinstance(value, bool) for value in flags.values()):
        raise ValueError(f"Invalid MCU dispatch boolean metadata for {name}")
    needs_ack = "true" if flags["needs_ack"] else "false"
    retransmit = "true" if flags["retransmit_on_dup"] else "false"
    check_dup = "true" if flags["check_dup"] else "false"

    if adapter == "member_no_payload":
        return f"&BridgeClass::_dispatchMemberNoPayload<{handler_ptr}, {needs_ack}, {retransmit}, {check_dup}>"
    if adapter == "member_with_msg":
        return f"&BridgeClass::_dispatchMemberWithMsg<{handler_ptr}, {payload_type}, {needs_ack}, {retransmit}, {check_dup}>"
    if adapter == "member_with_ctx_msg":
        return f"&BridgeClass::_dispatchMemberWithCtxMsg<{handler_ptr}, {payload_type}, {needs_ack}, {retransmit}, {check_dup}>"
    if adapter == "static_with_msg":
        return f"&BridgeClass::_dispatchStaticWithMsg<{handler_ptr}, {payload_type}, {needs_ack}, {retransmit}, {check_dup}>"
    if adapter == "target_with_msg":
        return f"&BridgeClass::_dispatchTargetWithMsg<{target_instance}, {handler_ptr}, {payload_type}, {needs_ack}, {retransmit}, {check_dup}>"
    return f"&BridgeClass::_dispatchTargetAction<{target_instance}, {handler_ptr}, {needs_ack}, {retransmit}, {check_dup}>"



def _extract_payloads_and_actions(
    pb_module: Any,
    all_descs: list[Any],
    opts: Any,
    env_desc: Any,
) -> dict[str, Any]:
    payload_fields: list[dict[str, str]] = []
    payload_structs: list[dict[str, str]] = []
    for f in env_desc.oneofs_by_name["payload_type"].fields:
        if f.message_type:
            item = {"name": f.message_type.name, "field": f.name}
            payload_fields.append(item)
            if f.message_type.name != "DaemonMetrics":
                payload_structs.append(item)
    payload_names = [p["name"] for p in payload_structs]
    mcu_names = set(payload_names) | {"RpcEnvelope"}
    all_structs = [{"name": n} for d in all_descs for n in d.message_types_by_name if n in mcu_names]

    action_items: dict[str, list[dict[str, Any]]] = {}
    for a in opts.Extensions[pb_module.actions]:
        if "_" in a.name:
            pfx, sfx = a.name.split("_", 1)
            action_items.setdefault(pfx, []).append({"name": sfx, "value": a.value, "description": a.description})
    grouped_actions = [
        {"class_name": "DatastoreAction" if p == "DATASTORE" else f"{p.lower().title()}Action", "action_items": items}
        for p, items in action_items.items()
    ]
    topics = [
        {
            "name": t.name,
            "value": t.value,
            "description": t.description,
            "aliases": list(t.aliases),
        }
        for t in opts.Extensions[pb_module.topics]
    ]
    ubus_methods = [
        {
            "name": m.name,
            "description": m.description,
            "args": [{"name": a.name, "type": a.type} for a in m.args],
        }
        for m in opts.Extensions[pb_module.ubus_methods]
    ]
    daemon_tasks = [
        {
            "name": t.name,
            "handler": t.handler,
            "critical": t.critical,
            "fatal_exception": t.fatal_exception,
            "condition_property": t.condition_property,
            "description": t.description,
        }
        for t in opts.Extensions[pb_module.daemon_tasks]
    ]
    return {
        "payload_fields": payload_fields,
        "payload_structs": payload_structs,
        "payload_names": payload_names,
        "all_structs": all_structs,
        "grouped_actions": grouped_actions,
        "topics": topics,
        "ubus_methods": ubus_methods,
        "daemon_tasks": daemon_tasks,
    }


def _extract_rpc_specs(
    pb_module: Any, svc_desc: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    rpc_specs: list[dict[str, Any]] = []
    streaming_rpc_specs: list[dict[str, Any]] = []
    rpc_aliases: dict[str, str] = {}
    for m in svc_desc.methods:
        cardinality = "Cardinality.UNARY_UNARY"
        is_streaming = False
        if m.client_streaming and m.server_streaming:
            cardinality = "Cardinality.STREAM_STREAM"
            is_streaming = True
        elif m.client_streaming:
            cardinality = "Cardinality.STREAM_UNARY"
            is_streaming = True
        elif m.server_streaming:
            cardinality = "Cardinality.UNARY_STREAM"
            is_streaming = True

        mo = m.output_type.GetOptions()
        err_init = mo.Extensions[pb_module.msg_error_init] if mo.HasExtension(pb_module.msg_error_init) else ""
        m_opts = m.GetOptions()
        rt = m_opts.Extensions[pb_module.rpc_topic] if m_opts.HasExtension(pb_module.rpc_topic) else ""
        if rt:
            rpc_aliases[rt] = m.name
        for a in m_opts.Extensions[pb_module.rpc_aliases]:
            rpc_aliases[a] = m.name
        rpc_cmd = (
            m_opts.Extensions[pb_module.rpc_command]
            if m_opts.HasExtension(pb_module.rpc_command)
            else f"CMD_{_snake_case(m.name).upper()}"
        )
        is_custom = (
            bool(m_opts.Extensions[pb_module.is_custom_handler])
            if m_opts.HasExtension(pb_module.is_custom_handler)
            else False
        )
        factory = f"lambda: pb.{m.output_type.name}({err_init})" if err_init else f"pb.{m.output_type.name}"
        item = {
            "name": m.name,
            "req_cls": m.input_type.name,
            "resp_cls": m.output_type.name,
            "default_factory": factory,
            "handler_name": _snake_case(m.name),
            "rpc_topic": rt,
            "rpc_command": rpc_cmd,
            "cardinality": cardinality,
            "is_custom_handler": is_custom,
        }
        if is_streaming:
            streaming_rpc_specs.append(item)
        else:
            rpc_specs.append(item)
    return rpc_specs, streaming_rpc_specs, rpc_aliases


def _build_command_operation(
    cmd: dict[str, Any],
    pb_module: Any,
    rpc_by_cmd: dict[str, Any],
    p_fields_map: dict[str, str],
) -> dict[str, Any]:
    c_name = cmd["name"]
    pb_msg = cmd.get("pb_message")
    pb_cls = pb_msg if (pb_msg and hasattr(pb_module, pb_msg)) else None
    matched_rpc = rpc_by_cmd.get(c_name)
    op: dict[str, Any] = {"name": c_name, "command_id": f"Command.{c_name}.value"}
    if pb_cls:
        op["pb_class"] = f"pb.{pb_cls}"
    if pb_msg and pb_msg in p_fields_map:
        op["payload_field"] = p_fields_map[pb_msg]
    if cmd.get("cloud_topic"):
        op["cloud_topic"] = cmd["cloud_topic"]
    if cmd.get("response_command"):
        op["expected_responses"] = [f"Command.{cmd['response_command']}.value"]
    if cmd.get("request_command"):
        op["paired_request_id"] = f"Command.{cmd['request_command']}.value"
    if cmd.get("auth_field"):
        op["auth_field"] = cmd["auth_field"]
    if cmd.get("requires_ack"):
        op["requires_ack"] = True
    if cmd.get("expects_direct_response"):
        op["expects_direct_response"] = True
    if cmd.get("pre_sync_allowed"):
        op["pre_sync_allowed"] = True
    if matched_rpc:
        op.update(
            {
                "rpc_name": matched_rpc["name"],
                "request_type": f"pb.{matched_rpc['req_cls']}",
                "response_type": f"pb.{matched_rpc['resp_cls']}",
                "default_factory": matched_rpc["default_factory"],
                "handler_name": matched_rpc["handler_name"],
                "cardinality": matched_rpc["cardinality"],
            }
        )
        if matched_rpc.get("rpc_topic"):
            op["rpc_topic"] = matched_rpc["rpc_topic"]
        if matched_rpc.get("is_custom_handler"):
            op["is_custom_handler"] = True
    return op


def _build_topic_or_payload_op(
    name: str,
    pb_module: Any,
    *,
    cloud_topic: str | None,
    payload_field: str | None,
    blob_field: str | None,
) -> dict[str, Any]:
    op: dict[str, Any] = {"name": name}
    if cloud_topic:
        op["cloud_topic"] = cloud_topic
    if hasattr(pb_module, name):
        op["pb_class"] = f"pb.{name}"
    if payload_field:
        op["payload_field"] = payload_field
    if blob_field:
        op["metrics_blob_field"] = blob_field
    return op


def _build_operations_catalog(
    commands: list[dict[str, Any]],
    rpc_specs: list[dict[str, Any]],
    rpc_by_cmd: dict[str, Any],
    p_fields_map: dict[str, str],
    msg_topics: dict[str, str],
    telem_map: dict[str, str],
    pb_module: Any,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = [
        _build_command_operation(cmd, pb_module, rpc_by_cmd, p_fields_map) for cmd in commands
    ]

    matched_rpc_names = {r["name"] for r in rpc_by_cmd.values()}
    operations.extend(
        {
            "name": rpc["name"],
            "rpc_name": rpc["name"],
            "request_type": f"pb.{rpc['req_cls']}",
            "response_type": f"pb.{rpc['resp_cls']}",
            "default_factory": rpc["default_factory"],
            "handler_name": rpc["handler_name"],
            "rpc_topic": rpc.get("rpc_topic"),
            "cardinality": rpc["cardinality"],
            "is_custom_handler": rpc.get("is_custom_handler", False),
        }
        for rpc in rpc_specs
        if rpc["name"] not in matched_rpc_names
    )

    seen_names = {op["name"] for op in operations}
    seen_names.update(op["rpc_name"] for op in operations if "rpc_name" in op)
    seen_names.update(op["pb_class"].removeprefix("pb.") for op in operations if "pb_class" in op)

    for msg_name, topic in msg_topics.items():
        act_name = msg_name.removesuffix("_ENUM")
        if act_name in seen_names:
            continue
        blob_field = next((bf for tm, bf in telem_map.items() if tm in topic), None) or (
            "system_status_blob" if "status" in topic else None
        )
        operations.append(
            _build_topic_or_payload_op(
                act_name,
                pb_module,
                cloud_topic=topic,
                payload_field=p_fields_map.get(act_name),
                blob_field=blob_field,
            )
        )
        seen_names.add(act_name)

    for msg_name, f_name in p_fields_map.items():
        if msg_name in seen_names:
            continue
        blob_field = next((bf for tm, bf in telem_map.items() if tm in msg_name.lower()), None)
        operations.append(
            _build_topic_or_payload_op(
                msg_name,
                pb_module,
                cloud_topic=msg_topics.get(msg_name),
                payload_field=f_name,
                blob_field=blob_field,
            )
        )
        seen_names.add(msg_name)

    return operations


_METRIC_NUMERIC_TYPES: Final[frozenset[int]] = frozenset(
    {
        FieldDescriptor.TYPE_DOUBLE,
        FieldDescriptor.TYPE_FLOAT,
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_FIXED64,
        FieldDescriptor.TYPE_FIXED32,
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_SFIXED32,
        FieldDescriptor.TYPE_SFIXED64,
        FieldDescriptor.TYPE_SINT32,
        FieldDescriptor.TYPE_SINT64,
        FieldDescriptor.TYPE_BOOL,
    }
)
_METRIC_UNSIGNED_INT_TYPES: Final[frozenset[int]] = frozenset(
    {
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_FIXED32,
        FieldDescriptor.TYPE_FIXED64,
    }
)
_METRIC_LABEL_FIELD_TYPES: Final[frozenset[int]] = frozenset(
    {
        FieldDescriptor.TYPE_STRING,
        FieldDescriptor.TYPE_BOOL,
        FieldDescriptor.TYPE_ENUM,
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT64,
    }
)


def _resolve_metric_value_field(field: Any, option: Any, pb_module: Any) -> Any:
    if field.is_repeated:
        if field.message_type is None or not option.value_field:
            raise ValueError(f"Repeated Prometheus metric source DaemonMetrics.{field.name} needs value_field")
        value_field = field.message_type.fields_by_name.get(option.value_field)
        if value_field is None:
            raise ValueError(
                f"Prometheus metric {option.prometheus_name!r} references unknown value field "
                f"{field.message_type.name}.{option.value_field}"
            )
    else:
        if option.value_field:
            raise ValueError(f"Scalar Prometheus metric source DaemonMetrics.{field.name} cannot set value_field")
        value_field = field

    if value_field.type not in _METRIC_NUMERIC_TYPES:
        raise ValueError(f"Prometheus metric {option.prometheus_name!r} must reference a numeric field")
    if (
        option.prometheus_kind == pb_module.PROMETHEUS_METRIC_KIND_COUNTER
        and value_field.type not in _METRIC_UNSIGNED_INT_TYPES
    ):
        raise ValueError(f"Prometheus counter {option.prometheus_name!r} must reference an unsigned integer field")
    return value_field


def _extract_metric_labels(field: Any, option: Any) -> tuple[list[dict[str, str | None]], tuple[tuple[str, str], ...]]:
    labels: list[dict[str, str | None]] = []
    label_signature: list[tuple[str, str]] = []
    label_names: set[str] = set()
    for label in option.labels:
        source_kind = label.WhichOneof("value_source")
        if not label.name or label.name == "device_id" or label.name in label_names:
            raise ValueError(f"Prometheus metric {option.prometheus_name!r} has an invalid or duplicate label name")
        label_names.add(label.name)
        if source_kind == "field":
            if not field.is_repeated or field.message_type is None:
                raise ValueError(
                    f"Prometheus metric {option.prometheus_name!r} can use field labels only "
                    "with repeated message sources"
                )
            label_field = field.message_type.fields_by_name.get(label.field)
            if label_field is None or label_field.type not in _METRIC_LABEL_FIELD_TYPES:
                raise ValueError(
                    f"Prometheus metric {option.prometheus_name!r} has an invalid label field {label.field!r}"
                )
            labels.append({"name": label.name, "field": label.field, "static_value": None})
            label_signature.append((label.name, "field"))
        elif source_kind == "static_value":
            labels.append({"name": label.name, "field": None, "static_value": label.static_value})
            label_signature.append((label.name, "static_value"))
        else:
            raise ValueError(f"Prometheus metric {option.prometheus_name!r} label {label.name!r} has no value source")
    return labels, tuple(label_signature)


def _extract_metric_specs(pb_module: Any, all_descs: list[Any]) -> list[dict[str, Any]]:
    """Build the gateway metric catalog from DaemonMetrics field options."""
    metrics_desc = next(
        message
        for descriptor in all_descs
        for message in descriptor.message_types_by_name.values()
        if message.name == "DaemonMetrics"
    )
    metric_kinds = {
        pb_module.PROMETHEUS_METRIC_KIND_GAUGE: "MetricKind.GAUGE",
        pb_module.PROMETHEUS_METRIC_KIND_COUNTER: "MetricKind.COUNTER",
    }
    metric_specs: list[dict[str, Any]] = []
    sources: set[str] = set()
    families: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {}

    for field in metrics_desc.fields:
        field_options = field.GetOptions()
        if not field_options.HasExtension(pb_module.telemetry_metric):
            continue

        option = field_options.Extensions[pb_module.telemetry_metric]
        if not option.prometheus_name:
            continue
        if re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", option.prometheus_name) is None:
            raise ValueError(
                f"Invalid Prometheus metric name on DaemonMetrics.{field.name}: {option.prometheus_name!r}"
            )
        if not option.prometheus_help:
            raise ValueError(f"Prometheus metric {option.prometheus_name!r} has no help text")
        if option.prometheus_kind not in metric_kinds:
            raise ValueError(f"Prometheus metric {option.prometheus_name!r} has an unsupported metric kind")
        if field.name in sources:
            raise ValueError(f"DaemonMetrics.{field.name} declares more than one Prometheus metric")
        sources.add(field.name)

        _resolve_metric_value_field(field, option, pb_module)
        labels, label_signature = _extract_metric_labels(field, option)

        kind_name = metric_kinds[option.prometheus_kind].removeprefix("MetricKind.").lower()
        signature = (kind_name, option.prometheus_help, label_signature)
        previous_signature = families.setdefault(option.prometheus_name, signature)
        if previous_signature != signature:
            raise ValueError(f"Prometheus metric family {option.prometheus_name!r} has conflicting field metadata")

        metric_specs.append(
            {
                "source_field": field.name,
                "metric_name": option.prometheus_name,
                "help": option.prometheus_help,
                "kind": metric_kinds[option.prometheus_kind],
                "value_field": option.value_field or None,
                "labels": labels,
            }
        )

    return metric_specs


def _extract_line_protocol_specs(pb_module: Any, all_descs: list[Any]) -> list[dict[str, Any]]:
    """Build ordered Line Protocol field projections from DaemonMetrics options."""
    metrics_desc = next(
        message
        for descriptor in all_descs
        for message in descriptor.message_types_by_name.values()
        if message.name == "DaemonMetrics"
    )
    field_kinds = {
        pb_module.LINE_PROTOCOL_FIELD_KIND_INTEGER: "LineProtocolFieldKind.INTEGER",
        pb_module.LINE_PROTOCOL_FIELD_KIND_FLOAT: "LineProtocolFieldKind.FLOAT",
        pb_module.LINE_PROTOCOL_FIELD_KIND_BOOLEAN: "LineProtocolFieldKind.BOOLEAN",
    }
    integer_types = {
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_FIXED64,
        FieldDescriptor.TYPE_FIXED32,
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_SFIXED32,
        FieldDescriptor.TYPE_SFIXED64,
        FieldDescriptor.TYPE_SINT32,
        FieldDescriptor.TYPE_SINT64,
        FieldDescriptor.TYPE_BOOL,
    }
    numeric_types = integer_types | {FieldDescriptor.TYPE_DOUBLE, FieldDescriptor.TYPE_FLOAT}
    float_types = {FieldDescriptor.TYPE_DOUBLE, FieldDescriptor.TYPE_FLOAT}
    line_fields: list[dict[str, Any]] = []
    line_names: set[str] = set()
    line_orders: set[int] = set()

    for field in metrics_desc.fields:
        field_options = field.GetOptions()
        if not field_options.HasExtension(pb_module.telemetry_metric):
            continue
        option = field_options.Extensions[pb_module.telemetry_metric]
        if not option.line_protocol_name:
            continue
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", option.line_protocol_name) is None:
            raise ValueError(f"Invalid Line Protocol field name: {option.line_protocol_name!r}")
        if field.is_repeated or field.type not in numeric_types:
            raise ValueError(f"Line Protocol field {option.line_protocol_name!r} must reference a numeric scalar")
        if option.line_protocol_kind not in field_kinds:
            raise ValueError(f"Line Protocol field {option.line_protocol_name!r} has an unsupported value kind")
        if option.line_protocol_kind == pb_module.LINE_PROTOCOL_FIELD_KIND_INTEGER and field.type not in integer_types:
            raise ValueError(f"Line Protocol integer field {option.line_protocol_name!r} must be integral")
        if option.line_protocol_kind == pb_module.LINE_PROTOCOL_FIELD_KIND_FLOAT and field.type not in float_types:
            raise ValueError(f"Line Protocol float field {option.line_protocol_name!r} must be floating-point")
        if (
            option.line_protocol_kind == pb_module.LINE_PROTOCOL_FIELD_KIND_BOOLEAN
            and field.type != FieldDescriptor.TYPE_BOOL
        ):
            raise ValueError(f"Line Protocol boolean field {option.line_protocol_name!r} must reference a bool")
        if option.line_protocol_name in line_names or option.line_protocol_order in line_orders:
            raise ValueError("Line Protocol field names and order values must be unique")
        line_names.add(option.line_protocol_name)
        line_orders.add(option.line_protocol_order)
        line_fields.append(
            {
                "source_field": field.name,
                "field_name": option.line_protocol_name,
                "kind": field_kinds[option.line_protocol_kind],
                "order": option.line_protocol_order,
            }
        )

    return sorted(line_fields, key=lambda item: item["order"])


def _extract_operations_context(
    pb_module: Any,
    all_descs: list[Any],
    opts: Any,
    commands: list[dict[str, Any]],
    env_desc: Any,
    svc_desc: Any,
) -> dict[str, Any]:
    payload_ctx = _extract_payloads_and_actions(pb_module, all_descs, opts, env_desc)
    payload_fields = payload_ctx["payload_fields"]
    p_fields_map = {p["name"]: p["field"] for p in payload_fields}

    rpc_specs, streaming_rpc_specs, rpc_aliases = _extract_rpc_specs(pb_module, svc_desc)
    cmd_by_name = {c["name"]: c for c in commands}
    rpc_by_cmd = {r["rpc_command"]: r for r in rpc_specs if r["rpc_command"] in cmd_by_name}

    telem_fields = pb_module.TelemetryReport.DESCRIPTOR.fields
    telem_map: dict[str, str] = {}
    default_telem: str | None = None
    for f in telem_fields:
        fo = f.GetOptions()
        if fo.HasExtension(pb_module.telemetry_topic_match):
            telem_map[fo.Extensions[pb_module.telemetry_topic_match]] = f.name
        elif fo.HasExtension(pb_module.telemetry_topic_default):
            default_telem = f.name

    msg_topics = {
        m_name: (m_desc.GetOptions().Extensions[pb_module.msg_cloud_topic])
        for d in all_descs
        for m_name, m_desc in d.message_types_by_name.items()
        if m_desc.GetOptions().HasExtension(pb_module.msg_cloud_topic)
    }
    msg_topics.update(
        {
            f"{e_name}_ENUM": (e_desc.GetOptions().Extensions[pb_module.enum_cloud_topic])
            for d in all_descs
            for e_name, e_desc in d.enum_types_by_name.items()
            if e_desc.GetOptions().HasExtension(pb_module.enum_cloud_topic)
        }
    )

    operations = _build_operations_catalog(
        commands, rpc_specs, rpc_by_cmd, p_fields_map, msg_topics, telem_map, pb_module
    )
    metric_specs = _extract_metric_specs(pb_module, all_descs)
    line_protocol_fields = _extract_line_protocol_specs(pb_module, all_descs)

    return {
        "grouped_actions": payload_ctx["grouped_actions"],
        "all_structs": payload_ctx["all_structs"],
        "payload_fields": payload_fields,
        "payload_structs": payload_ctx["payload_structs"],
        "payload_names": payload_ctx["payload_names"],
        "operations": operations,
        "metrics": metric_specs,
        "line_protocol_fields": line_protocol_fields,
        "rpc_aliases": rpc_aliases,
        "ack_commands": [c for c in commands if c["requires_ack"]],
        "pre_sync_allowed_commands": [c for c in commands if c["pre_sync_allowed"]],
        "response_only_commands": [c for c in commands if c["expects_direct_response"]],
        "topics": payload_ctx["topics"],
        "message_topics": msg_topics,
        "telemetry_topic_field_map": telem_map,
        "telemetry_default_field": default_telem,
        "ubus_methods": payload_ctx["ubus_methods"],
        "daemon_tasks": payload_ctx["daemon_tasks"],
        "streaming_rpc_specs": streaming_rpc_specs,
    }


def _extract_runtime_config_context(
    pb_module: Any,
    opts: Any,
    py_consts: list[dict[str, Any]],
) -> dict[str, Any]:
    cfg_fields = _load_runtime_config_fields(pb_module.DESCRIPTOR, pb_module)
    cfg_names = {c["name"] for c in py_consts}
    rt_consts = [
        {
            "name": f"DEFAULT_{f.name.upper()}",
            "type": f.field_type,
            "value": (
                f'b"{f.raw_default}"'
                if f.field_type == "bytes"
                else f'"{f.default_value}"' if f.field_type == "str" else str(f.default_value)
            ),
        }
        for f in cfg_fields
        if f.default_value is not None
        and f.field_type != "list"
        and f.config_default_ref is None
        and f"DEFAULT_{f.name.upper()}" not in cfg_names
    ]
    cl_rt_consts = [
        {"name": f"DEFAULT_{f.name.upper()}", "type": f.field_type, "value": f.default_value}
        for f in cfg_fields
        if f.client_constant and f.default_value is not None
    ]

    topic_auth_map: dict[tuple[str, str], str] = {}
    auth_fields = [f.name for f in pb_module.TopicAuthorization.DESCRIPTOR.fields]
    for n in auth_fields:
        for prefix in ("bridge_", "gpio_", "system_", "shell_"):
            if n.startswith(prefix):
                topic_auth_map[(prefix.removesuffix("_").upper(), n.removeprefix(prefix).upper())] = n
                break
        else:
            parts = n.split("_", 1)
            if len(parts) == 2:
                topic_auth_map[(parts[0].upper(), parts[1].upper())] = n

    status_reasons = _message_to_dict(opts.Extensions[pb_module.status_reasons])
    for f in pb_module.StatusReasons.DESCRIPTOR.fields:
        status_reasons.setdefault(f.name, f.name)

    hw_dict = _message_to_dict(opts.Extensions[pb_module.hardware])

    return {
        "runtime_config_constants": rt_consts,
        "client_runtime_config_constants": cl_rt_consts,
        "runtime_config_fields": cfg_fields,
        "topic_auth_map": topic_auth_map,
        "capabilities": {c.name: c.value for c in opts.Extensions[pb_module.capabilities]},
        "architectures": {a.name: a.value for a in opts.Extensions[pb_module.architectures]},
        "architecture_display_names": {
            a.name: a.display_name for a in opts.Extensions[pb_module.architectures] if a.display_name
        },
        "cloud_defaults": _message_to_dict(opts.Extensions[pb_module.cloud_defaults]),
        "status_reasons": status_reasons,
        "spi_bit_orders": [{"name": v.name, "value": v.number} for v in pb_module.SpiBitOrder.DESCRIPTOR.values],
        "spi_data_modes": [{"name": v.name, "value": v.number} for v in pb_module.SpiDataMode.DESCRIPTOR.values],
        "hardware": hw_dict,
    }


def build_protocol_context(spec_or_pb: Any, version: str) -> dict[str, Any]:
    parsed = Version(version)
    target: object = spec_or_pb
    if isinstance(target, dict):
        pb_module: Any = cast(dict[str, Any], target).get("pb_module", target)
    else:
        pb_module: Any = target

    all_descs: list[Any] = [pb_module.DESCRIPTOR, *pb_module.DESCRIPTOR.dependencies]
    opts: Any = next(d.GetOptions() for d in all_descs if d.GetOptions().HasExtension(pb_module.constants))
    cmd_enum: Any = next(d.enum_types_by_name["Command"] for d in all_descs if "Command" in d.enum_types_by_name)
    status_enum: Any = next(d.enum_types_by_name["Status"] for d in all_descs if "Status" in d.enum_types_by_name)
    env_name = "RpcEnvelope"
    env_desc: Any = next(d.message_types_by_name[env_name] for d in all_descs if env_name in d.message_types_by_name)
    svc_desc: Any = next(d.services_by_name["LocalBridge"] for d in all_descs if "LocalBridge" in d.services_by_name)

    consts_ctx = _extract_constants_and_handshake(pb_module, opts, parsed.major, parsed.minor, parsed.micro)
    commands, statuses = _extract_commands_and_statuses(pb_module, cmd_enum, status_enum)
    ops_ctx = _extract_operations_context(pb_module, all_descs, opts, commands, env_desc, svc_desc)
    cfg_ctx = _extract_runtime_config_context(pb_module, opts, consts_ctx["python_constants"])
    mcu_dispatch_entries = _build_mcu_dispatch_entries(commands, statuses)

    return consts_ctx | ops_ctx | cfg_ctx | {
        "commands": commands,
        "statuses": statuses,
        "mcu_dispatch_entries": mcu_dispatch_entries,
    }


def load_spec_from_proto(proto_path: Path, pb_module: Any = None) -> dict[str, Any]:
    if pb_module is None:
        proto_dir = str(proto_path.parent)
        if proto_dir not in sys.path:
            sys.path.insert(0, proto_dir)
        cfg_mod = importlib.import_module("config_schema_pb2")
        wire_mod = importlib.import_module("mcu_wire_pb2")
        cloud_mod = importlib.import_module("cloud_gateway_pb2")

        class _UnifiedProto:
            def __init__(self) -> None:
                for m in (cfg_mod, wire_mod, cloud_mod):
                    for k in dir(m):
                        if not k.startswith("__"):
                            setattr(self, k, getattr(m, k))
                self.DESCRIPTOR = cloud_mod.DESCRIPTOR

        pb_module = _UnifiedProto()
    ctx = build_protocol_context(pb_module, "0.0.0")
    return {
        "commands": ctx["commands"],
        "runtime_config_fields": ctx["runtime_config_fields"],
        "pb_module": pb_module,
        "spec_context": ctx,
    }


class JinjaGenerator:
    def __init__(self) -> None:
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            keep_trailing_newline=True,
        )
        cast(dict[str, Any], self.env.filters)["cpp_digits"] = _format_cpp_digit

    def render_template(
        self,
        template_name: str,
        context: dict[str, Any],
        out_path: Path,
        *,
        create_parent: bool = False,
        executable: bool = False,
        format_python: bool = False,
    ) -> None:
        if create_parent:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = self.env.get_template(template_name).render(**context)
        if format_python or out_path.suffix in (".py", ".pyi"):
            rendered = black.format_str(rendered, mode=black.Mode())
        if not out_path.is_file() or out_path.read_text(encoding="utf-8") != rendered:
            out_path.write_text(rendered, encoding="utf-8")
        if executable:
            out_path.chmod(0o755)


def _resolve_proto_path(spec_path: Path) -> Path:
    resolved_spec = spec_path.resolve()
    if resolved_spec.is_dir():
        candidate = resolved_spec / "cloud_gateway.proto"
        return candidate.resolve() if candidate.exists() else resolved_spec
    if resolved_spec.name in ("mcubridge.proto", "mcubridge.toml"):
        candidate = resolved_spec.parent / "cloud_gateway.proto"
        if candidate.exists() or not resolved_spec.exists():
            return candidate.resolve()
    return resolved_spec


def _template_target_specs(args: Any, repo_root: Path) -> list[tuple[str, str, Path | None, bool, bool]]:
    return [
        ("rpc_protocol.h.j2", "common", args.cpp, False, False),
        (
            "rpc_hw_config.h.j2",
            "common",
            args.cpp.parent / "rpc_hw_config.h" if args.cpp else None,
            False,
            False,
        ),
        ("rpc_structs.h.j2", "common", args.cpp_structs, False, False),
        (
            "rpc_dispatch_table.inc.j2",
            "common",
            args.cpp.parent / "rpc_dispatch_table.inc" if args.cpp else None,
            False,
            False,
        ),
        ("protocol.py.j2", "python", args.py, False, True),
        ("protocol_client.py.j2", "client", args.py_client, False, True),
        (
            "mcubridge_uci.j2",
            "common",
            repo_root / "luci-app-mcubridge" / "root" / "etc" / "config" / "mcubridge",
            False,
            False,
        ),
        ("defaults_sh.j2", "common", repo_root / "mcubridge" / "scripts" / "defaults.sh", True, False),
        (
            "config_schema_json.j2",
            "common",
            repo_root
            / "luci-app-mcubridge"
            / "htdocs"
            / "luci-static"
            / "resources"
            / "view"
            / "mcubridge"
            / "config_schema.json",
            False,
            False,
        ),
    ]


def expected_generation_outputs(args: Any, *, repo_root: Path = REPO_ROOT) -> list[Path]:
    explicit_outputs = [path for path in (args.cpp, args.cpp_structs, args.py, args.py_client) if path]
    outputs = list(explicit_outputs)
    if args.cpp:
        for stem in ("config_schema", "mcu_wire"):
            outputs.extend((args.cpp.parent / f"{stem}.pb.h", args.cpp.parent / f"{stem}.pb.c"))

    python_output_dirs = {path.parent for path in (args.py, args.py_client) if path}
    for output_dir in python_output_dirs:
        for stem in ("config_schema", "mcu_wire", "cloud_gateway"):
            outputs.extend((output_dir / f"{stem}_pb2.py", output_dir / f"{stem}_pb2.pyi"))
        outputs.extend(
            (
                output_dir / "mcubridge_pb2.py",
                output_dir / "mcubridge_pb2.pyi",
                output_dir / "mcubridge_grpc.py",
            )
        )

    explicit_output_dirs = {path.parent for path in explicit_outputs}
    outputs.extend(
        target
        for _, _, target, _, _ in _template_target_specs(args, repo_root)
        if target and (target.parent.exists() or target in explicit_outputs or target.parent in explicit_output_dirs)
    )
    return list(dict.fromkeys(outputs))


def _generation_input_paths(args: Any, proto_path: Path, repo_root: Path) -> list[Path]:
    templates_dir = repo_root / "tools" / "protocol" / "templates"
    template_paths = sorted(templates_dir.glob("*.j2"))
    if not template_paths:
        raise FileNotFoundError(f"No protocol templates found in {templates_dir}")

    proto_files = sorted(proto_path.parent.glob("*.proto"))
    options_files = sorted(proto_path.parent.glob("*.options"))
    input_paths = [
        proto_path.resolve(),
        Path(__file__).resolve(),
        *proto_files,
        *options_files,
        proto_path.parent / "buf.yaml",
        proto_path.parent / "buf.gen.yaml",
        repo_root / ".github" / "actions" / "setup-protoc" / "action.yml",
        *template_paths,
    ]
    version_path = repo_root / "VERSION"
    if version_path.is_file():
        input_paths.append(version_path)

    unique_paths = list(dict.fromkeys(path.resolve() for path in input_paths))
    missing = [path for path in unique_paths if not path.is_file()]
    if missing:
        missing_list = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(f"Protocol generation inputs are missing: {missing_list}")
    return unique_paths


def check_incremental_build(args: Any, version: str, *, repo_root: Path = REPO_ROOT) -> tuple[bool, Path, str]:
    proto_path = _resolve_proto_path(args.spec)
    input_paths = _generation_input_paths(args, proto_path, repo_root)
    repo_resolved = repo_root.resolve()
    h = hashlib.sha256()
    for input_path in input_paths:
        input_name = (
            input_path.relative_to(repo_resolved).as_posix()
            if input_path.is_relative_to(repo_resolved)
            else input_path.as_posix()
        )
        h.update(input_name.encode("utf-8"))
        h.update(input_path.stat().st_size.to_bytes(8, "big"))
        with input_path.open("rb") as f:
            h.update(hashlib.file_digest(f, "sha256").digest())
    buf_version = subprocess.run(["buf", "--version"], check=True, capture_output=True, text=True).stdout.strip()
    if not buf_version:
        raise RuntimeError("buf --version returned an empty version")
    h.update(b"buf-version\0")
    h.update(buf_version.encode("utf-8"))
    h.update(version.encode("utf-8"))
    current_hash = h.hexdigest()

    hash_file = proto_path.parent / ".mcubridge.proto.hash"
    outputs_exist = all(output.is_file() for output in expected_generation_outputs(args, repo_root=repo_root))
    up_to_date = outputs_exist and hash_file.is_file() and hash_file.read_text(encoding="utf-8").strip() == current_hash
    return up_to_date, hash_file, current_hash


def _dispatch_generated_file(
    src: Path,
    targets: list[Path | None],
    transform: Callable[[str], str] | None = None,
    dest_name: str | None = None,
) -> None:
    if not src.exists():
        raise FileNotFoundError(f"Expected generated protocol artifact is missing: {src}")
    data = transform(src.read_text(encoding="utf-8")).encode("utf-8") if transform else src.read_bytes()
    out_name = dest_name or src.name
    for target_dir in filter(None, targets):
        target = target_dir / out_name
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
    src.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class GenerationArgs:
    spec: Path
    cpp: Path | None
    cpp_structs: Path | None
    py: Path | None
    py_client: Path | None


def _extract_pyi_symbol_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            names.append(node.name)
        elif (
            isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and not node.target.id.startswith("_")
        ):
            names.append(node.target.id)
        elif isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name) and not t.id.startswith("_"))
    return names


def _generate_unified_pyi(py_dir: Path) -> str:
    lines = ['"""Consolidated Python protobuf stubs for MCU Bridge v2."""\n']
    for stem in ("config_schema_pb2", "mcu_wire_pb2", "cloud_gateway_pb2"):
        stub_path = py_dir / f"{stem}.pyi"
        if not stub_path.is_file():
            continue
        tree = ast.parse(stub_path.read_text(encoding="utf-8"))
        names = _extract_pyi_symbol_names(tree)
        if names:
            reexports = ", ".join(f"{n} as {n}" for n in sorted(set(names)))
            lines.append(f"from .{stem} import ({reexports})\n")
    return "\n".join(lines)


cli = typer.Typer(help="Protocol binding generator for MCU Bridge v2.", add_completion=False)


@cli.command()
def main(
    spec_file: Annotated[Path, typer.Option("--spec", help="Protocol specification file (.proto)")],
    cpp: Annotated[Path | None, typer.Option("--cpp", help="C++ header output")] = None,
    cpp_structs: Annotated[Path | None, typer.Option("--cpp-structs", help="C++ structs output")] = None,
    py: Annotated[Path | None, typer.Option("--py", help="Python output")] = None,
    py_client: Annotated[Path | None, typer.Option("--py-client", help="Python client output")] = None,
) -> None:
    args = GenerationArgs(spec=spec_file, cpp=cpp, cpp_structs=cpp_structs, py=py, py_client=py_client)

    gen = JinjaGenerator()
    version = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "0.0.0"
    if version == "0.0.0":
        sys.stderr.write(f"Warning: VERSION file not found at {VERSION_PATH}, using fallback.\n")

    proto_path = _resolve_proto_path(args.spec)
    if not proto_path.is_file():
        raise FileNotFoundError(f"Protocol specification not found: {proto_path}")

    up_to_date, hash_file, current_hash = check_incremental_build(args, version)
    if up_to_date:
        sys.stderr.write("Protocol bindings up-to-date, skipping generation.\n")
        return

    sys.stderr.write(f"Generating protocol bindings from {proto_path} via buf...\n")
    run_buf_generate(proto_path.parent)

    # Now load the compiled descriptor
    proto_spec = load_spec_from_proto(proto_path)
    context = build_protocol_context(proto_spec, version)

    # Dispatch compiled protobuf and stub artifacts
    py_targets = [args.py.parent if args.py else None, args.py_client.parent if args.py_client else None]
    cpp_targets = [args.cpp.parent if args.cpp else None]

    def _sanitize_pb2(text: str) -> str:
        text = re.sub(
            r"^import (config_schema_pb2|mcu_wire_pb2|cloud_gateway_pb2)\b",
            r"from . import \1",
            text,
            flags=re.MULTILINE,
        )
        return re.sub(
            r"^from (config_schema_pb2|mcu_wire_pb2|cloud_gateway_pb2)\b",
            r"from .\1",
            text,
            flags=re.MULTILINE,
        )

    for stem in ("config_schema", "mcu_wire"):
        _dispatch_generated_file(
            proto_path.parent / f"{stem}.pb.h",
            cpp_targets,
            lambda t: t.replace("#include <pb.h>", '#include "../pb.h"'),
        )
        _dispatch_generated_file(proto_path.parent / f"{stem}.pb.c", cpp_targets)

    for stem in ("config_schema", "mcu_wire", "cloud_gateway"):
        _dispatch_generated_file(proto_path.parent / f"{stem}_pb2.py", py_targets, _sanitize_pb2)
        _dispatch_generated_file(proto_path.parent / f"{stem}_pb2.pyi", py_targets, _sanitize_pb2)

    unified_py = (
        '"""Consolidated Python protobuf entrypoint for MCU Bridge v2."""\n'
        "from . import config_schema_pb2 as config_schema_pb2\n"
        "from . import mcu_wire_pb2 as mcu_wire_pb2\n"
        "from . import cloud_gateway_pb2 as cloud_gateway_pb2\n"
        "from .config_schema_pb2 import *\n"
        "from .mcu_wire_pb2 import *\n"
        "from .cloud_gateway_pb2 import *\n\n"
        "DESCRIPTOR = cloud_gateway_pb2.DESCRIPTOR\n"
    )
    for py_dir in filter(None, py_targets):
        (py_dir / "mcubridge_pb2.py").write_text(unified_py, encoding="utf-8")
        (py_dir / "mcubridge_pb2.pyi").write_text(_generate_unified_pyi(py_dir), encoding="utf-8")

    def _sanitize_grpc_bindings(text: str) -> str:
        text = re.sub(
            r"^import (config_schema_pb2|mcu_wire_pb2|cloud_gateway_pb2)\b",
            r"from . import \1",
            text,
            flags=re.MULTILINE,
        )
        return text.replace("if typing.TYPE_CHECKING:\n    import grpclib.server", "import grpclib.server")

    _dispatch_generated_file(
        proto_path.parent / "cloud_gateway_grpc.py",
        py_targets,
        _sanitize_grpc_bindings,
        dest_name="mcubridge_grpc.py",
    )
    for grpc_file in proto_path.parent.glob("*_grpc.py"):
        grpc_file.unlink(missing_ok=True)

    # Render Jinja2 artifacts declaratively
    py_context = context | {
        "constants": context["python_constants"],
        "handshake_constants": context["python_handshake_constants"],
    }
    py_client_context = context | {"constants": context["client_constants"]}
    template_contexts = {"common": context, "python": py_context, "client": py_client_context}
    explicit_outputs = (args.cpp, args.cpp_structs, args.py, args.py_client)
    for template_name, context_name, target_path, executable, is_py in _template_target_specs(args, REPO_ROOT):
        if target_path and (
            target_path.parent.exists()
            or target_path in explicit_outputs
            or target_path.parent in {path.parent for path in explicit_outputs if path}
        ):
            gen.render_template(
                template_name,
                template_contexts[context_name],
                target_path,
                create_parent=True,
                executable=executable,
                format_python=is_py,
            )
            sys.stderr.write(f"Generated {target_path}\n")

    missing_outputs = [output for output in expected_generation_outputs(args) if not output.is_file()]
    if missing_outputs:
        missing_list = ", ".join(str(output) for output in missing_outputs)
        raise RuntimeError(f"Protocol generation did not create expected artifacts: {missing_list}")

    # Save hash for incremental compilation
    hash_file.write_text(current_hash, encoding="utf-8")


if __name__ == "__main__":
    cli()
