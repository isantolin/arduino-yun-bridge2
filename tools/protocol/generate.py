#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Jinja2: Declarative templates for C++ and Python outputs.

Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import importlib.util
import os
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
from pw_rpc.descriptors import Service

REQUIRED_DEPS = [
    "jinja2",
    "google.protobuf",
    "nanopb",
    "mypy_protobuf",
    "grpclib",
    "black",
    "typer",
    "packaging",
    "pw_rpc",
    "pw_transfer",
]
MISSING_DEPS: list[str] = [dep for dep in REQUIRED_DEPS if importlib.util.find_spec(dep.split(".")[0]) is None]
HAS_BUF: bool = shutil.which("buf") is not None
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = Path(__file__).parent / "templates"
VERSION_PATH = REPO_ROOT / "VERSION"

if MISSING_DEPS or not HAS_BUF:
    sys.stderr.write("ERROR: Missing dependencies required for protocol generation.\n")
    for dep in MISSING_DEPS:
        sys.stderr.write(f"  - {dep} (Python)\n")
    if not HAS_BUF:
        sys.stderr.write("  - buf (Buf CLI not found in PATH)\n")
    sys.exit(1)


def _resolve_nanopb_protoc_dir() -> Path | None:
    """Locate the directory containing nanopb's bundled protoc binary."""
    spec = importlib.util.find_spec("nanopb")
    if spec and spec.origin:
        candidate = Path(spec.origin).parent / "generator"
        if (candidate / "protoc").exists():
            return candidate
    return None


def run_buf_generate(proto_dir: Path) -> None:
    """Run ``buf generate`` declaratively."""
    env = os.environ.copy()
    py_bin_dir = str(Path(sys.executable).parent)
    if sys.prefix != sys.base_prefix:
        env["PATH"] = f"{py_bin_dir}:{env.get('PATH', '')}"
    nanopb_dir = _resolve_nanopb_protoc_dir()
    if nanopb_dir:
        env["PATH"] = f"{env['PATH']}:{nanopb_dir}"
    try:
        subprocess.run(["buf", "generate"], check=True, capture_output=True, text=True, cwd=str(proto_dir), env=env)
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
    has_literal = opts.HasExtension(pb_module.config_default)
    has_ref = opts.HasExtension(pb_module.config_default_ref)
    if has_literal and has_ref:
        raise ValueError(f"RuntimeConfig field '{field_desc.name}' has both default value and default reference")
    if not has_ref:
        return (opts.Extensions[pb_module.config_default] if has_literal else None), None
    reference = opts.Extensions[pb_module.config_default_ref]
    source_name, separator, field_name = reference.partition(".")
    source = default_sources.get(source_name)
    if not separator or not field_name or source is None or field_name not in source.DESCRIPTOR.fields_by_name:
        raise ValueError(f"Invalid RuntimeConfig default reference '{reference}'")
    return getattr(source, field_name), reference


def _opt_ext(opts: Any, ext: Any, default: Any = None) -> Any:
    return opts.Extensions[ext] if opts.HasExtension(ext) else default


def _load_runtime_config_fields(file_desc: Any, pb_module: Any) -> list[ConfigFieldDef]:
    all_descs = [file_desc, *file_desc.dependencies]
    runtime_config_desc = next(
        (d.message_types_by_name["RuntimeConfig"] for d in all_descs if "RuntimeConfig" in d.message_types_by_name),
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

    fields: list[ConfigFieldDef] = []
    for field_desc in runtime_config_desc.fields:
        opts = field_desc.GetOptions()
        cfg_default, config_default_ref = _resolve_field_default(field_desc, opts, pb_module, default_sources)
        if (
            getattr(field_desc, "is_repeated", False)
            or getattr(field_desc, "label", None) == FieldDescriptor.LABEL_REPEATED
        ):
            py_type, typed_val = "list", []
        elif field_desc.type in _FIELD_CONVERTERS:
            py_type, converter = _FIELD_CONVERTERS[field_desc.type]
            typed_val = converter(cfg_default)
        else:
            py_type, typed_val = "message", None
        fields.append(
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
    return fields


def _format_py_const_val(py_name: str, py_type: str, val: Any) -> Any:
    if py_name == "FRAME_DELIMITER":
        return f"bytes([ {val} ])"
    if py_type == "float":
        return float(val)
    return f'b"{val}"' if py_type == "bytes" else f'"{val}"' if py_type == "str" else val


def _extract_pb_obj_consts(
    pb_obj: Any, pb_module: Any, *, collect_client: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    cpp_out: list[dict[str, Any]] = []
    py_out: list[dict[str, Any]] = []
    client_out: list[dict[str, Any]] = []
    for f in pb_obj.DESCRIPTOR.fields:
        fo = f.GetOptions()
        val = getattr(pb_obj, f.name)
        cpp_n, cpp_t = fo.Extensions[pb_module.cpp_name], fo.Extensions[pb_module.cpp_type]
        if cpp_n:
            cpp_out.append({"name": cpp_n, "type": cpp_t, "value": val})
        py_n, py_t = fo.Extensions[pb_module.py_name], fo.Extensions[pb_module.py_type]
        if py_n:
            c_def = {"name": py_n, "type": py_t, "value": _format_py_const_val(py_n, py_t, val)}
            py_out.append(c_def)
            if collect_client and fo.Extensions[pb_module.client_constant]:
                client_out.append(c_def)
    return cpp_out, py_out, client_out


def _extract_constants_and_handshake(
    pb_module: Any, opts: Any, v_major: int, v_minor: int, v_patch: int
) -> dict[str, Any]:
    c_opt = opts.Extensions[pb_module.constants]
    hw_opt = opts.Extensions[pb_module.hardware]
    hs_opt = opts.Extensions[pb_module.handshake]

    cpp_consts, py_consts, client_consts = _extract_pb_obj_consts(c_opt, pb_module, collect_client=True)
    hw_cpp, hw_py, _ = _extract_pb_obj_consts(hw_opt, pb_module)
    cpp_consts.extend(hw_cpp)
    py_consts.extend(hw_py)

    for n, v in (
        ("FIRMWARE_VERSION_MAJOR", v_major),
        ("FIRMWARE_VERSION_MINOR", v_minor),
        ("FIRMWARE_VERSION_PATCH", v_patch),
    ):
        cpp_consts.append({"name": n, "type": "uint8_t", "value": v})
        py_consts.append({"name": n, "type": "int", "value": v})
        client_consts.append({"name": n, "type": "int", "value": v})

    cloud_suffixes = _message_to_dict(opts.Extensions[pb_module.cloud_suffixes])
    for f in pb_module.CloudSuffixes.DESCRIPTOR.fields:
        cloud_suffixes.setdefault(f.name, f.name)
    for n, v in cloud_suffixes.items():
        cpp_consts.append({"name": f"RPC_CLOUD_SUFFIX_{n.upper()}", "type": "const char*", "value": f'"{v}"'})
        py_consts.append({"name": f"CLOUD_SUFFIX_{n.upper()}", "type": "str", "value": f'"{v}"'})

    hs_cpp, hs_py, _ = _extract_pb_obj_consts(hs_opt, pb_module)
    handshake_dict: dict[str, Any] = {}
    for key in (
        "hkdf_salt",
        "hkdf_info_auth",
        "hkdf_info_session",
        "hkdf_info_response",
    ):
        raw_str: str = getattr(hs_opt, key)
        handshake_dict[key] = raw_str
        handshake_dict[f"{key}_bytes"] = ", ".join(f"0x{ord(c):02X}" for c in raw_str)
        handshake_dict[f"{key}_len"] = len(raw_str)

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


def _extract_statuses(pb_module: Any, status_enum: Any) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for value in status_enum.values:
        if value.name == "STATUS_UNSPECIFIED":
            continue
        status_options = value.GetOptions().Extensions[pb_module.status_opts]
        status: dict[str, Any] = {
            "name": value.name,
            "value": value.number,
            "description": status_options.description,
        }
        statuses.append(status)
    return statuses


def _extract_payloads_and_actions(pb_module: Any, all_descs: list[Any], opts: Any) -> dict[str, Any]:
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
        "ubus_methods": ubus_methods,
        "daemon_tasks": daemon_tasks,
    }


def _resolve_pb_module_name(name: str, pb_module: Any) -> str | None:
    wire_mod = getattr(pb_module, "_wire_mod", None)
    cloud_mod = getattr(pb_module, "_cloud_mod", None)
    cfg_mod = getattr(pb_module, "_cfg_mod", None)
    if wire_mod is not None and hasattr(wire_mod, name):
        return f"mcu_wire_pb2.{name}"
    if cloud_mod is not None and hasattr(cloud_mod, name):
        return f"cloud_gateway_pb2.{name}"
    if cfg_mod is not None and hasattr(cfg_mod, name):
        return f"config_schema_pb2.{name}"
    if hasattr(pb_module, name):
        return f"mcu_wire_pb2.{name}"
    return None


def _extract_rpc_specs(pb_module: Any, svc_desc: Any) -> list[dict[str, Any]]:
    rpc_specs: list[dict[str, Any]] = []
    for m in svc_desc.methods:
        if m.client_streaming or m.server_streaming:
            continue
        mo = m.output_type.GetOptions()
        err_init = _opt_ext(mo, pb_module.msg_error_init, "")
        m_opts = m.GetOptions()
        is_custom = bool(_opt_ext(m_opts, pb_module.is_custom_handler, False))
        mod_target = _resolve_pb_module_name(m.output_type.name, pb_module) or f"mcu_wire_pb2.{m.output_type.name}"
        item: dict[str, Any] = {
            "name": m.name,
            "input_type": m.input_type.full_name,
            "output_type": m.output_type.full_name,
            "default_factory": f"lambda: {mod_target}({err_init})" if err_init else None,
            "handler_name": _snake_case(m.name),
            "is_custom_handler": is_custom,
            "cloud_topic": _opt_ext(m_opts, pb_module.cloud_topic, ""),
            "auth_field": _opt_ext(m_opts, pb_module.auth_field, ""),
            "mcu_handler": _opt_ext(m_opts, pb_module.mcu_handler, ""),
            "pre_sync_allowed": bool(_opt_ext(m_opts, pb_module.pre_sync_allowed, False)),
        }
        rpc_specs.append(item)
    return rpc_specs


def _match_local_bridge_rpc(
    mcu_method: dict[str, Any],
    rpc_specs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    method_name = mcu_method["method_name"]
    input_type = mcu_method["input_type"]
    output_type = mcu_method["output_type"]
    for rpc in rpc_specs:
        if rpc["name"] == method_name:
            return rpc
    if input_type != "rpc.pb.SubscribeRequest":
        for rpc in rpc_specs:
            if rpc["input_type"] == input_type:
                return rpc
    if output_type not in ("google.protobuf.Empty", "rpc.pb.GenericResponse"):
        for rpc in rpc_specs:
            if rpc["output_type"] == output_type:
                return rpc
    return None


def _build_mcu_operation(
    mcu_method: dict[str, Any],
    matched_rpc: dict[str, Any] | None,
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "name": mcu_method["name"],
        "service_name": mcu_method["service_name"],
        "method_name": mcu_method["method_name"],
    }
    for str_key in ("cloud_topic", "auth_field", "mcu_handler"):
        if mcu_method.get(str_key):
            op[str_key] = mcu_method[str_key]
    if mcu_method.get("pre_sync_allowed"):
        op["pre_sync_allowed"] = True
    if matched_rpc:
        op["rpc_name"] = matched_rpc["name"]
        op["handler_name"] = matched_rpc["handler_name"]
        if matched_rpc.get("default_factory"):
            op["default_factory"] = matched_rpc["default_factory"]
        if matched_rpc.get("is_custom_handler"):
            op["is_custom_handler"] = True
    return op


def _build_topic_or_payload_op(
    name: str,
    pb_module: Any,
    *,
    cloud_topic: str | None,
    blob_field: str | None,
) -> dict[str, Any]:
    op: dict[str, Any] = {"name": name}
    if cloud_topic:
        op["cloud_topic"] = cloud_topic
    mod_name = _resolve_pb_module_name(name, pb_module)
    if mod_name is not None:
        op["pb_class"] = mod_name
    if blob_field:
        op["metrics_blob_field"] = blob_field
    return op


def _build_operations_catalog(
    mcu_methods: list[dict[str, Any]],
    rpc_specs: list[dict[str, Any]],
    msg_topics: dict[str, str],
    telem_map: dict[str, str],
    pb_module: Any,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    matched_rpc_names: set[str] = set()
    for mcu_m in mcu_methods:
        matched_rpc = _match_local_bridge_rpc(mcu_m, rpc_specs)
        if matched_rpc is not None:
            matched_rpc_names.add(matched_rpc["name"])
        operations.append(_build_mcu_operation(mcu_m, matched_rpc))

    for rpc in rpc_specs:
        if rpc["name"] in matched_rpc_names:
            continue
        rpc_op: dict[str, Any] = {
            "name": rpc["name"],
            "rpc_name": rpc["name"],
            "default_factory": rpc["default_factory"],
            "handler_name": rpc["handler_name"],
            "is_custom_handler": rpc.get("is_custom_handler", False),
        }
        for str_key in ("cloud_topic", "auth_field", "mcu_handler"):
            if rpc.get(str_key):
                rpc_op[str_key] = rpc[str_key]
        if rpc.get("pre_sync_allowed"):
            rpc_op["pre_sync_allowed"] = True
        operations.append(rpc_op)

    seen_names = {op["name"] for op in operations}
    seen_names.update(op["rpc_name"] for op in operations if "rpc_name" in op)

    for msg_name, topic in msg_topics.items():
        act_name = msg_name.removesuffix("_ENUM")
        if act_name in seen_names:
            continue
        blob_field = next((bf for tm, bf in telem_map.items() if tm in topic), None) or (
            "system_status_blob" if "status" in topic else None
        )
        operations.append(_build_topic_or_payload_op(act_name, pb_module, cloud_topic=topic, blob_field=blob_field))
        seen_names.add(act_name)

    return operations


def _find_daemon_metrics_desc(all_descs: list[Any]) -> Any:
    return next(msg for desc in all_descs for msg in desc.message_types_by_name.values() if msg.name == "DaemonMetrics")


def _extract_metric_specs(pb_module: Any, all_descs: list[Any]) -> list[dict[str, Any]]:
    """Build the gateway metric catalog from DaemonMetrics field options."""
    metric_kinds = {
        pb_module.PROMETHEUS_METRIC_KIND_GAUGE: "MetricKind.GAUGE",
        pb_module.PROMETHEUS_METRIC_KIND_COUNTER: "MetricKind.COUNTER",
    }
    metric_specs: list[dict[str, Any]] = []
    for field in _find_daemon_metrics_desc(all_descs).fields:
        field_options = field.GetOptions()
        if not field_options.HasExtension(pb_module.telemetry_metric):
            continue
        option = field_options.Extensions[pb_module.telemetry_metric]
        if not option.prometheus_name:
            continue
        labels = [
            {
                "name": lbl.name,
                "field": lbl.field if lbl.WhichOneof("value_source") == "field" else None,
                "static_value": lbl.static_value if lbl.WhichOneof("value_source") == "static_value" else None,
            }
            for lbl in option.labels
        ]
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
    field_kinds = {
        pb_module.LINE_PROTOCOL_FIELD_KIND_INTEGER: "LineProtocolFieldKind.INTEGER",
        pb_module.LINE_PROTOCOL_FIELD_KIND_FLOAT: "LineProtocolFieldKind.FLOAT",
        pb_module.LINE_PROTOCOL_FIELD_KIND_BOOLEAN: "LineProtocolFieldKind.BOOLEAN",
    }
    line_fields = [
        {
            "source_field": field.name,
            "field_name": opt.line_protocol_name,
            "kind": field_kinds[opt.line_protocol_kind],
            "order": opt.line_protocol_order,
        }
        for field in _find_daemon_metrics_desc(all_descs).fields
        if field.GetOptions().HasExtension(pb_module.telemetry_metric)
        and (opt := field.GetOptions().Extensions[pb_module.telemetry_metric]).line_protocol_name
    ]
    return sorted(line_fields, key=lambda item: item["order"])


def _extract_operations_context(
    pb_module: Any,
    all_descs: list[Any],
    opts: Any,
    svc_desc: Any,
) -> dict[str, Any]:
    payload_ctx = _extract_payloads_and_actions(pb_module, all_descs, opts)
    rpc_specs = _extract_rpc_specs(pb_module, svc_desc)

    telem_map: dict[str, str] = {}
    default_telem: str | None = None
    for f in pb_module.TelemetryReport.DESCRIPTOR.fields:
        fo = f.GetOptions()
        if fo.HasExtension(pb_module.telemetry_topic_match):
            telem_map[fo.Extensions[pb_module.telemetry_topic_match]] = f.name
        elif fo.HasExtension(pb_module.telemetry_topic_default):
            default_telem = f.name

    msg_topics = {
        m_name: m_desc.GetOptions().Extensions[pb_module.msg_cloud_topic]
        for d in all_descs
        for m_name, m_desc in d.message_types_by_name.items()
        if m_desc.GetOptions().HasExtension(pb_module.msg_cloud_topic)
    }
    msg_topics.update(
        {
            f"{e_name}_ENUM": e_desc.GetOptions().Extensions[pb_module.enum_cloud_topic]
            for d in all_descs
            for e_name, e_desc in d.enum_types_by_name.items()
            if e_desc.GetOptions().HasExtension(pb_module.enum_cloud_topic)
        }
    )

    excluded_services = ("CloudBridge", "LocalBridge", "GatewayControl", "EdgeControl", "DeviceTelemetry")
    mcu_services = [s for d in all_descs for s in d.services_by_name.values() if s.name not in excluded_services]
    mcu_methods: list[dict[str, Any]] = []
    for s_desc in mcu_services:
        svc = Service.from_descriptor(s_desc)
        for method_desc in s_desc.methods:
            method_options = method_desc.GetOptions()
            mcu_methods.append(
                {
                    "name": f"{svc.full_name}/{method_desc.name}",
                    "service_name": svc.full_name,
                    "method_name": method_desc.name,
                    "input_type": method_desc.input_type.full_name,
                    "output_type": method_desc.output_type.full_name,
                    "auth_field": _opt_ext(method_options, pb_module.auth_field, ""),
                    "mcu_handler": _opt_ext(method_options, pb_module.mcu_handler, ""),
                    "cloud_topic": _opt_ext(method_options, pb_module.cloud_topic, ""),
                    "pre_sync_allowed": bool(_opt_ext(method_options, pb_module.pre_sync_allowed, False)),
                    "description": _opt_ext(method_options, pb_module.method_description, ""),
                }
            )

    operations = _build_operations_catalog(mcu_methods, rpc_specs, msg_topics, telem_map, pb_module)
    return {
        "operations": operations,
        "metrics": _extract_metric_specs(pb_module, all_descs),
        "line_protocol_fields": _extract_line_protocol_specs(pb_module, all_descs),
        "message_topics": msg_topics,
        "telemetry_topic_field_map": telem_map,
        "telemetry_default_field": default_telem,
        "ubus_methods": payload_ctx["ubus_methods"],
        "daemon_tasks": payload_ctx["daemon_tasks"],
    }


def _extract_runtime_config_context(pb_module: Any, opts: Any, py_consts: list[dict[str, Any]]) -> dict[str, Any]:
    cfg_fields = _load_runtime_config_fields(pb_module.DESCRIPTOR, pb_module)
    cfg_names = {c["name"] for c in py_consts}
    rt_consts = [
        {
            "name": f"DEFAULT_{f.name.upper()}",
            "type": f.field_type,
            "value": _format_py_const_val(
                f.name, f.field_type, f.raw_default if f.field_type == "bytes" else f.default_value
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
    for n in (f.name for f in pb_module.TopicAuthorization.DESCRIPTOR.fields):
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
        "hardware": _message_to_dict(opts.Extensions[pb_module.hardware]),
    }


def build_protocol_context(spec_or_pb: Any, version: str) -> dict[str, Any]:
    parsed = Version(version)
    target: object = spec_or_pb
    pb_module: Any = cast(dict[str, Any], target).get("pb_module", target) if isinstance(target, dict) else target

    all_descs: list[Any] = [pb_module.DESCRIPTOR, *pb_module.DESCRIPTOR.dependencies]
    opts: Any = next(d.GetOptions() for d in all_descs if d.GetOptions().HasExtension(pb_module.constants))
    status_enum: Any = next(d.enum_types_by_name["Status"] for d in all_descs if "Status" in d.enum_types_by_name)
    svc_desc: Any = next(d.services_by_name["LocalBridge"] for d in all_descs if "LocalBridge" in d.services_by_name)

    consts_ctx = _extract_constants_and_handshake(pb_module, opts, parsed.major, parsed.minor, parsed.micro)
    statuses = _extract_statuses(pb_module, status_enum)
    ops_ctx = _extract_operations_context(pb_module, all_descs, opts, svc_desc)
    cfg_ctx = _extract_runtime_config_context(pb_module, opts, consts_ctx["python_constants"])

    return (
        consts_ctx
        | ops_ctx
        | cfg_ctx
        | {
            "statuses": statuses,
        }
    )


def load_spec_from_proto(proto_path: Path, pb_module: Any = None) -> dict[str, Any]:
    if pb_module is None:
        proto_dir = str(proto_path.parent)
        if proto_dir not in sys.path:
            sys.path.insert(0, proto_dir)
        for mod_name in ("config_schema_pb2", "mcu_wire_pb2", "cloud_gateway_pb2"):
            if mod_name in sys.modules:
                del sys.modules[mod_name]
        cfg_mod = importlib.import_module("config_schema_pb2")
        wire_mod = importlib.import_module("mcu_wire_pb2")
        cloud_mod = importlib.import_module("cloud_gateway_pb2")

        class _UnifiedProto:
            def __init__(self) -> None:
                self._wire_mod = wire_mod
                self._cloud_mod = cloud_mod
                self._cfg_mod = cfg_mod
                for m in (cfg_mod, wire_mod, cloud_mod):
                    for k in dir(m):
                        if not k.startswith("__"):
                            setattr(self, k, getattr(m, k))
                self.DESCRIPTOR = cloud_mod.DESCRIPTOR

        pb_module = _UnifiedProto()
    ctx = build_protocol_context(pb_module, "0.0.0")
    return {
        "operations": ctx["operations"],
        "runtime_config_fields": ctx["runtime_config_fields"],
        "pb_module": pb_module,
        "spec_context": ctx,
    }


class JinjaGenerator:
    def __init__(self) -> None:
        self.env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), keep_trailing_newline=True)
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
    cpp_dir = args.cpp.parent if args.cpp else None
    luci_root = repo_root / "luci-app-mcubridge"
    specs: list[tuple[str, str, Path | None, bool, bool]] = [
        ("rpc_hw_config.h.j2", "common", cpp_dir / "rpc_hw_config.h" if cpp_dir else None, False, False),
        ("protocol.py.j2", "python", args.py, False, True),
        ("protocol_client.py.j2", "client", args.py_client, False, True),
        ("mcubridge_uci.j2", "common", luci_root / "root/etc/config/mcubridge", False, False),
        ("defaults_sh.j2", "common", repo_root / "mcubridge/scripts/defaults.sh", True, False),
        (
            "config_schema_json.j2",
            "common",
            luci_root / "htdocs/luci-static/resources/view/mcubridge/config_schema.json",
            False,
            False,
        ),
    ]
    if args.cpp:
        specs.append(("rpc_protocol.h.j2", "common", args.cpp, False, False))
    return specs


def expected_generation_outputs(args: Any, *, repo_root: Path = REPO_ROOT) -> list[Path]:
    explicit_outputs = [path for path in (args.cpp, args.py, args.py_client) if path]
    outputs = list(explicit_outputs)
    if args.cpp:
        for stem in ("config_schema", "mcu_wire"):
            outputs.extend((args.cpp.parent / f"{stem}.pb.h", args.cpp.parent / f"{stem}.pb.c"))
        outputs.append(repo_root / "mcubridge-library-arduino/src/protocol/mcu_wire.rpc.pb.h")

    for output_dir in {path.parent for path in (args.py, args.py_client) if path}:
        for stem in ("config_schema", "mcu_wire", "cloud_gateway"):
            outputs.extend((output_dir / f"{stem}_pb2.py", output_dir / f"{stem}_pb2.pyi"))
        outputs.extend(
            (
                output_dir / "gateway" / "gateway_control_pb2.py",
                output_dir / "gateway" / "gateway_control_pb2.pyi",
                output_dir / "gateway" / "__init__.py",
            )
        )
        outputs.append(output_dir / "mcubridge_grpc.py")

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

    input_paths = [
        proto_path.resolve(),
        Path(__file__).resolve(),
        *sorted(proto_path.parent.rglob("*.proto")),
        *sorted(proto_path.parent.rglob("*.options")),
        *sorted(proto_path.parent.rglob("*.pwpb_options")),
        proto_path.parent / "buf.yaml",
        proto_path.parent / "buf.gen.yaml",
        repo_root / ".github/actions/setup-protoc/action.yml",
        repo_root / "tools/protocol/protoc_gen_mcu_rpc.py",
        *template_paths,
    ]
    version_path = repo_root / "VERSION"
    if version_path.is_file():
        input_paths.append(version_path)

    unique_paths = list(dict.fromkeys(path.resolve() for path in input_paths))
    missing = [path for path in unique_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Protocol generation inputs are missing: {', '.join(str(p) for p in missing)}")
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
    py: Path | None
    py_client: Path | None


cli = typer.Typer(help="Protocol binding generator for MCU Bridge v2.", add_completion=False)


@cli.command()
def main(
    spec_file: Annotated[Path, typer.Option("--spec", help="Protocol specification file (.proto)")],
    cpp: Annotated[Path | None, typer.Option("--cpp", help="C++ header output")] = None,
    py: Annotated[Path | None, typer.Option("--py", help="Python output")] = None,
    py_client: Annotated[Path | None, typer.Option("--py-client", help="Python client output")] = None,
) -> None:
    args = GenerationArgs(spec=spec_file, cpp=cpp, py=py, py_client=py_client)
    install_sh = REPO_ROOT / "mcubridge-library-arduino" / "tools" / "install.sh"
    if install_sh.is_file():
        install_sh.chmod(0o755)
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

    proto_spec = load_spec_from_proto(proto_path)
    context = build_protocol_context(proto_spec, version)
    py_targets = [args.py.parent if args.py else None, args.py_client.parent if args.py_client else None]
    cpp_targets = [args.cpp.parent if args.cpp else None]

    def _sanitize_pb2(text: str) -> str:
        text = re.sub(
            r"^import (config_schema_pb2|mcu_wire_pb2|cloud_gateway_pb2|gateway_control_pb2)\b",
            r"from . import \1",
            text,
            flags=re.MULTILINE,
        )
        return re.sub(
            r"^from (config_schema_pb2|mcu_wire_pb2|cloud_gateway_pb2|gateway_control_pb2)\b",
            r"from .\1",
            text,
            flags=re.MULTILINE,
        )

    for stem in ("config_schema", "mcu_wire"):
        _dispatch_generated_file(
            proto_path.parent / f"{stem}.pb.h",
            cpp_targets,
            lambda t, s=stem: t.replace("#include <pb.h>", f'#include "../pb.h"\n\ntypedef pb_byte_t {s}_pb_byte_t;'),
        )
        _dispatch_generated_file(proto_path.parent / f"{stem}.pb.c", cpp_targets)

    for stem in ("config_schema", "mcu_wire", "cloud_gateway"):
        _dispatch_generated_file(proto_path.parent / f"{stem}_pb2.py", py_targets, _sanitize_pb2)
        _dispatch_generated_file(proto_path.parent / f"{stem}_pb2.pyi", py_targets, _sanitize_pb2)

    gateway_proto_dir = proto_path.parent / "gateway"
    gateway_targets = [target / "gateway" if target else None for target in py_targets]
    for target in filter(None, gateway_targets):
        target.mkdir(parents=True, exist_ok=True)
        init_file = target / "__init__.py"
        init_file.write_text('"""Pigweed Gateway protocol bindings."""\n', encoding="utf-8")
    _dispatch_generated_file(gateway_proto_dir / "gateway_control_pb2.py", gateway_targets, _sanitize_pb2)
    _dispatch_generated_file(gateway_proto_dir / "gateway_control_pb2.pyi", gateway_targets, _sanitize_pb2)

    _dispatch_generated_file(
        proto_path.parent / "cloud_gateway_grpc.py",
        py_targets,
        lambda t: _sanitize_pb2(t).replace(
            "if typing.TYPE_CHECKING:\n    import grpclib.server", "import grpclib.server"
        ),
        dest_name="mcubridge_grpc.py",
    )
    for grpc_file in proto_path.parent.rglob("*_grpc.py"):
        grpc_file.unlink(missing_ok=True)
    for nanopb_file in (gateway_proto_dir / "gateway_control.pb.h", gateway_proto_dir / "gateway_control.pb.c"):
        nanopb_file.unlink(missing_ok=True)

    py_context = context | {
        "constants": context["python_constants"],
        "handshake_constants": context["python_handshake_constants"],
    }
    template_contexts = {
        "common": context,
        "python": py_context,
        "client": context | {"constants": context["client_constants"]},
    }
    explicit_outputs = (args.cpp, args.py, args.py_client)
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
        missing_str = ", ".join(map(str, missing_outputs))
        raise RuntimeError(f"Protocol generation did not create expected artifacts: {missing_str}")

    hash_file.write_text(current_hash, encoding="utf-8")


if __name__ == "__main__":
    cli()
