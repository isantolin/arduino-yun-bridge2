#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Jinja2: Declarative templates for C++ and Python outputs.

Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from __future__ import annotations
from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import os
import re
import shutil
import site
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, Any

from google.protobuf.json_format import MessageToDict
from jinja2 import Environment, FileSystemLoader
from packaging.version import Version
import typer

# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY VALIDATION (CRITICAL)
# ═════════════════════════════════════════════════════════════════════════════
REQUIRED_DEPS = ["jinja2", "google.protobuf", "nanopb"]
MISSING_DEPS: list[str] = []
UNTYPED_LIBS = [
    "cobs",
    "prometheus_client",
    "serialx",
    "uci",
    "uvloop",
    "typer",
    "lmdb",
    "nanopb",
]

for dep in REQUIRED_DEPS:
    if importlib.util.find_spec(dep.split(".")[0]) is None:
        MISSING_DEPS.append(dep)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def resolve_protoc_bin() -> Path:
    p_bin = (REPO_ROOT / "bin" / "protoc").resolve()
    if p_bin.exists():
        return p_bin
    which_p = shutil.which("protoc")
    if which_p:
        return Path(which_p)
    try:
        import nanopb

        nanopb_p = Path(nanopb.__file__).parent / "generator" / "protoc"
        if nanopb_p.exists():
            return nanopb_p
    except Exception:
        pass
    return Path("protoc")


def _check_has_protoc(bin_path: Path) -> bool:
    try:
        return subprocess.run([str(bin_path), "--version"], capture_output=True, check=False).returncode == 0
    except FileNotFoundError:
        return False


protoc_bin: Path = resolve_protoc_bin()
HAS_PROTOC: bool = _check_has_protoc(protoc_bin)

if MISSING_DEPS or not HAS_PROTOC:
    sys.stderr.write("\n" + "!" * 80 + "\n")
    sys.stderr.write("ERROR: Missing dependencies required for protocol generation:\n")
    for dep in MISSING_DEPS:
        sys.stderr.write(f"  - {dep} (Python)\n")
    if not HAS_PROTOC:
        sys.stderr.write("  - protoc (System binary or local ./bin/protoc missing)\n")
    sys.stderr.write("\nTo fix this, run:\n")
    if MISSING_DEPS:
        sys.stderr.write(f"  pip install {' '.join(MISSING_DEPS)}\n")
    if not HAS_PROTOC:
        sys.stderr.write("  Check README for local protoc installation instructions.\n")
    sys.stderr.write("!" * 80 + "\n\n")
    sys.exit(1)
# ═════════════════════════════════════════════════════════════════════════════


# Mappings and helper functions for reflective protocol constant generation.


def cmd_name_to_pb_class(cmd_name: str) -> str:
    """Convert CMD_X_Y style command name to CamelCase class name."""
    clean_name = cmd_name.removeprefix("CMD_")
    return "".join("Response" if seg == "RESP" else seg.capitalize() for seg in clean_name.split("_"))


@dataclass
class CommandDef:
    name: str
    value: int
    directions: list[str]
    category: str | None = None
    description: str | None = None
    requires_ack: bool = False
    expects_direct_response: bool = False
    cloud_topic: str | None = None


@dataclass
class StatusDef:
    name: str
    value: int
    description: str


@dataclass
class ProtocolSpec:
    constants: dict[str, Any]
    hardware: dict[str, Any]
    commands: list[CommandDef]
    statuses: list[StatusDef]
    handshake: dict[str, Any]
    cloud_subscriptions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    topics: list[dict[str, Any]]
    capabilities: dict[str, int]
    architectures: dict[str, int]
    data_formats: dict[str, str]
    cloud_suffixes: dict[str, str]
    cloud_defaults: dict[str, str]
    status_reasons: dict[str, str]
    architecture_display_names: dict[str, str]
    message_topics: dict[str, str]
    constants_opt: Any = None
    hardware_opt: Any = None
    handshake_opt: Any = None
    data_formats_opt: Any = None
    pb_module: Any = None


def load_spec_from_proto(proto_path: Path) -> ProtocolSpec:
    proto_dir = str(proto_path.parent)
    if proto_dir not in sys.path:
        sys.path.insert(0, proto_dir)

    mcubridge_pb2 = importlib.import_module("mcubridge_pb2")
    file_desc = mcubridge_pb2.DESCRIPTOR
    options = file_desc.GetOptions()

    constants_opt = options.Extensions[mcubridge_pb2.constants]
    hardware_opt = options.Extensions[mcubridge_pb2.hardware]
    handshake_opt = options.Extensions[mcubridge_pb2.handshake]
    data_formats_opt = options.Extensions[mcubridge_pb2.data_formats]
    cloud_suffixes_opt = options.Extensions[mcubridge_pb2.cloud_suffixes]
    cloud_defaults_opt = options.Extensions[mcubridge_pb2.cloud_defaults]
    status_reasons_opt = options.Extensions[mcubridge_pb2.status_reasons]
    cloud_subscriptions_opt = options.Extensions[mcubridge_pb2.cloud_subscriptions]
    topics_opt = options.Extensions[mcubridge_pb2.topics]
    actions_opt = options.Extensions[mcubridge_pb2.actions]
    architectures_opt = options.Extensions[mcubridge_pb2.architectures]
    capabilities_opt = options.Extensions[mcubridge_pb2.capabilities]

    constants = MessageToDict(
        constants_opt, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )
    hardware = MessageToDict(hardware_opt, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
    handshake = MessageToDict(
        handshake_opt, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )
    data_formats = MessageToDict(
        data_formats_opt, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )
    cloud_suffixes = MessageToDict(
        cloud_suffixes_opt, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )
    for field_desc in mcubridge_pb2.CloudSuffixes.DESCRIPTOR.fields:
        if not cloud_suffixes.get(field_desc.name):
            cloud_suffixes[field_desc.name] = field_desc.name

    cloud_defaults = MessageToDict(
        cloud_defaults_opt, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )
    status_reasons = MessageToDict(
        status_reasons_opt, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )
    for field_desc in mcubridge_pb2.StatusReasons.DESCRIPTOR.fields:
        if not status_reasons.get(field_desc.name):
            status_reasons[field_desc.name] = field_desc.name

    cloud_subscriptions: list[dict[str, Any]] = []
    for sub in cloud_subscriptions_opt:
        sub_dict = MessageToDict(sub, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
        if not sub_dict.get("qos"):
            sub_dict["qos"] = 1
        cloud_subscriptions.append(sub_dict)
    topics = [
        MessageToDict(t, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
        for t in topics_opt
    ]
    actions = [
        MessageToDict(a, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
        for a in actions_opt
    ]

    architectures: dict[str, int] = {}
    architecture_display_names: dict[str, str] = {}
    for arch in architectures_opt:
        architectures[arch.name] = arch.value
        if arch.display_name:
            architecture_display_names[arch.name] = arch.display_name

    capabilities: dict[str, int] = {}
    for cap in capabilities_opt:
        capabilities[cap.name] = cap.value

    # Load Command enum
    command_enum_desc = file_desc.enum_types_by_name["Command"]
    commands: list[CommandDef] = []
    for val in command_enum_desc.values:
        if val.name == "CMD_UNSPECIFIED":
            continue
        opts = val.GetOptions().Extensions[mcubridge_pb2.cmd_opts]
        commands.append(
            CommandDef(
                name=val.name,
                value=val.number,
                directions=list(opts.directions),
                category=opts.category or None,
                description=opts.description or None,
                requires_ack=opts.requires_ack,
                expects_direct_response=opts.expects_direct_response,
                cloud_topic=opts.cloud_topic or None,
            )
        )

    # Load Message CLOUD topics
    message_topics: dict[str, str] = {}
    for msg_name, msg_desc in file_desc.message_types_by_name.items():
        opts = msg_desc.GetOptions()
        if opts.HasExtension(mcubridge_pb2.msg_cloud_topic):
            message_topics[msg_name] = opts.Extensions[mcubridge_pb2.msg_cloud_topic]

    # Load Enum CLOUD topics (like Status)
    for enum_name, enum_desc in file_desc.enum_types_by_name.items():
        opts = enum_desc.GetOptions()
        if opts.HasExtension(mcubridge_pb2.enum_cloud_topic):
            # For enums, we treat it as a special mapping or just add to message_topics with a prefix
            message_topics[f"{enum_name}_ENUM"] = opts.Extensions[mcubridge_pb2.enum_cloud_topic]

    # Load Status enum
    status_enum_desc = file_desc.enum_types_by_name["Status"]
    statuses: list[StatusDef] = []
    for val in status_enum_desc.values:
        if val.name == "STATUS_UNSPECIFIED":
            continue
        opts = val.GetOptions().Extensions[mcubridge_pb2.status_opts]
        statuses.append(StatusDef(name=val.name, value=val.number, description=opts.description))

    spec = ProtocolSpec(
        constants=constants,
        hardware=hardware,
        commands=commands,
        statuses=statuses,
        handshake=handshake,
        cloud_subscriptions=cloud_subscriptions,
        actions=actions,
        topics=topics,
        capabilities=capabilities,
        architectures=architectures,
        data_formats=data_formats,
        cloud_suffixes=cloud_suffixes,
        cloud_defaults=cloud_defaults,
        status_reasons=status_reasons,
        architecture_display_names=architecture_display_names,
        message_topics=message_topics,
    )
    spec.constants_opt = constants_opt
    spec.hardware_opt = hardware_opt
    spec.handshake_opt = handshake_opt
    spec.data_formats_opt = data_formats_opt
    spec.pb_module = mcubridge_pb2
    return spec


TEMPLATE_DIR = Path(__file__).parent / "templates"
VERSION_PATH = REPO_ROOT / "VERSION"


class JinjaGenerator:
    def __init__(self) -> None:
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            keep_trailing_newline=True,
        )
        self.env.filters["cpp_digits"] = self._cpp_digit_separator
        self.env.filters["snakecase"] = self._snake_case

    @staticmethod
    def _cpp_digit_separator(value: object) -> str:
        """Format integers >= 10'000 with C++14 digit separators."""
        if not isinstance(value, int) or abs(value) < 10_000:
            return str(value)
        s = str(abs(value))
        parts: list[str] = []
        while s:
            parts.append(s[-3:])
            s = s[:-3]
        result = "'".join(reversed(parts))
        return f"-{result}" if value < 0 else result

    @staticmethod
    def _snake_case(s: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()

    def generate_cpp_header(self, spec: ProtocolSpec, out_path: Path, version: str) -> None:
        template = self.env.get_template("rpc_protocol.h.j2")

        parsed_version = Version(version)
        v_major, v_minor, v_patch = parsed_version.major, parsed_version.minor, parsed_version.micro

        constants: list[dict[str, Any]] = []
        # Reflection from spec.constants_opt descriptor fields
        for field in spec.constants_opt.DESCRIPTOR.fields:
            opts = field.GetOptions()
            cpp_name = opts.Extensions[spec.pb_module.cpp_name]
            cpp_type = opts.Extensions[spec.pb_module.cpp_type]
            if cpp_name:
                val = getattr(spec.constants_opt, field.name)
                constants.append({"name": cpp_name, "type": cpp_type, "value": val})

        # Reflection from spec.hardware_opt descriptor fields
        for field in spec.hardware_opt.DESCRIPTOR.fields:
            opts = field.GetOptions()
            cpp_name = opts.Extensions[spec.pb_module.cpp_name]
            cpp_type = opts.Extensions[spec.pb_module.cpp_type]
            if cpp_name:
                val = getattr(spec.hardware_opt, field.name)
                constants.append({"name": cpp_name, "type": cpp_type, "value": val})

        # Append version constants
        constants.append({"name": "FIRMWARE_VERSION_MAJOR", "type": "uint8_t", "value": v_major})
        constants.append({"name": "FIRMWARE_VERSION_MINOR", "type": "uint8_t", "value": v_minor})
        constants.append({"name": "FIRMWARE_VERSION_PATCH", "type": "uint8_t", "value": v_patch})

        hs = spec.handshake
        handshake_constants: list[dict[str, Any]] = []
        # Reflection from spec.handshake_opt descriptor fields
        for field in spec.handshake_opt.DESCRIPTOR.fields:
            opts = field.GetOptions()
            cpp_name = opts.Extensions[spec.pb_module.cpp_name]
            cpp_type = opts.Extensions[spec.pb_module.cpp_type]
            if cpp_name:
                val = getattr(spec.handshake_opt, field.name)
                handshake_constants.append({"name": cpp_name, "type": cpp_type, "value": val})

        handshake_data = {
            "hkdf_salt": hs["hkdf_salt"],
            "hkdf_salt_bytes": ", ".join(f"0x{ord(c):02X}" for c in hs["hkdf_salt"]),
            "hkdf_salt_len": len(hs["hkdf_salt"]),
            "hkdf_info_auth": hs["hkdf_info_auth"],
            "hkdf_info_auth_bytes": ", ".join(f"0x{ord(c):02X}" for c in hs["hkdf_info_auth"]),
            "hkdf_info_auth_len": len(hs["hkdf_info_auth"]),
            "hkdf_info_session": hs["hkdf_info_session"],
            "hkdf_info_session_bytes": ", ".join(f"0x{ord(c):02X}" for c in hs["hkdf_info_session"]),
            "hkdf_info_session_len": len(hs["hkdf_info_session"]),
        }

        render = template.render(
            constants=constants,
            handshake_constants=handshake_constants,
            handshake=handshake_data,
            capabilities=spec.capabilities,
            architectures=spec.architectures,
            statuses=spec.statuses,
            commands=spec.commands,
            ack_commands=[c for c in spec.commands if c.requires_ack],
            status_reasons=spec.status_reasons,
        )
        out_path.write_text(render, encoding="utf-8")

    def generate_cpp_structs(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("rpc_structs.h.j2")
        options_path = (REPO_ROOT / "tools" / "protocol" / "mcubridge.options").resolve()
        options_content = options_path.read_text(encoding="utf-8")
        skipped_messages = set(re.findall(r"rpc\.pb\.(\w+)\s+skip_message:true", options_content))

        # 1. Extract ALL message names via Protobuf Descriptor reflection
        file_desc = spec.pb_module.DESCRIPTOR
        all_msg_names = list(file_desc.message_types_by_name.keys())
        all_structs = [
            {"name": name} for name in all_msg_names if name not in skipped_messages and name != "RpcContainer"
        ]

        # 2. Extract messages inside RpcEnvelope oneof via Descriptor oneof reflection
        envelope_desc = file_desc.message_types_by_name.get("RpcEnvelope")
        payload_structs: list[dict[str, str]] = []
        if envelope_desc and "payload_type" in envelope_desc.oneofs_by_name:
            oneof_desc = envelope_desc.oneofs_by_name["payload_type"]
            for field_desc in oneof_desc.fields:
                if field_desc.message_type and field_desc.message_type.name not in skipped_messages:
                    payload_structs.append({"name": field_desc.message_type.name, "field": field_desc.name})

        payload_names = [s["name"] for s in payload_structs]
        render = template.render(all_structs=all_structs, payload_structs=payload_structs, payload_names=payload_names)
        out_path.write_text(render, encoding="utf-8")

    def generate_cpp_hw_config(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("rpc_hw_config.h.j2")
        render = template.render(hardware=spec.hardware)
        out_path.write_text(render, encoding="utf-8")

    def _extract_python_constants(self, spec: ProtocolSpec) -> list[dict[str, Any]]:
        constants: list[dict[str, Any]] = []
        # Reflection from spec.constants_opt descriptor fields
        for field in spec.constants_opt.DESCRIPTOR.fields:
            opts = field.GetOptions()
            py_name = opts.Extensions[spec.pb_module.py_name]
            py_type = opts.Extensions[spec.pb_module.py_type]
            if py_name:
                val = getattr(spec.constants_opt, field.name)
                if py_name == "FRAME_DELIMITER":
                    constants.append({"name": py_name, "type": py_type, "value": f"bytes([ {val} ])"})
                else:
                    constants.append({"name": py_name, "type": py_type, "value": val})

        # Reflection from spec.hardware_opt descriptor fields
        for field in spec.hardware_opt.DESCRIPTOR.fields:
            opts = field.GetOptions()
            py_name = opts.Extensions[spec.pb_module.py_name]
            py_type = opts.Extensions[spec.pb_module.py_type]
            if py_name:
                val = getattr(spec.hardware_opt, field.name)
                constants.append({"name": py_name, "type": py_type, "value": val})

        # Reflection from spec.data_formats_opt descriptor fields
        for field in spec.data_formats_opt.DESCRIPTOR.fields:
            opts = field.GetOptions()
            py_name = opts.Extensions[spec.pb_module.py_name]
            py_type = opts.Extensions[spec.pb_module.py_type]
            if py_name:
                val = getattr(spec.data_formats_opt, field.name)
                constants.append({"name": py_name, "type": py_type, "value": f'"{val}"'})

        # Cloud suffixes
        for key, val in spec.cloud_suffixes.items():
            py_name = f"CLOUD_SUFFIX_{key.upper()}"
            constants.append({"name": py_name, "type": "str", "value": f'"{val}"'})

        return constants

    def _extract_python_handshake_constants(self, spec: ProtocolSpec) -> list[dict[str, Any]]:
        handshake_constants: list[dict[str, Any]] = []
        # Reflection from spec.handshake_opt descriptor fields
        for field in spec.handshake_opt.DESCRIPTOR.fields:
            opts = field.GetOptions()
            py_name = opts.Extensions[spec.pb_module.py_name]
            py_type = opts.Extensions[spec.pb_module.py_type]
            if py_name:
                val = getattr(spec.handshake_opt, field.name)
                if py_type == "str":
                    formatted_val = f'"{val}"'
                elif py_type == "bytes":
                    formatted_val = f'b"{val}"'
                else:
                    formatted_val = val
                handshake_constants.append({"name": py_name, "type": py_type, "value": formatted_val})
        return handshake_constants

    def _group_actions(self, spec: ProtocolSpec) -> list[dict[str, Any]]:
        grouped_actions: list[dict[str, Any]] = []
        action_map: dict[str, list[dict[str, Any]]] = {}
        for act in spec.actions:
            if "_" not in act["name"]:
                continue
            prefix, suffix = act["name"].split("_", 1)
            action_map.setdefault(prefix, []).append(
                {
                    "name": suffix,
                    "value": act["value"],
                    "description": act["description"],
                }
            )

        for prefix, items in action_map.items():
            cls_name = "DatastoreAction" if prefix == "DATASTORE" else f"{prefix.lower().title()}Action"
            grouped_actions.append({"class_name": cls_name, "action_items": items})
        return grouped_actions

    def _process_python_subscriptions(self, spec: ProtocolSpec) -> list[dict[str, Any]]:
        valid_topic_names = {t["name"] for t in spec.topics}
        subscriptions: list[dict[str, Any]] = []
        for sub in spec.cloud_subscriptions:
            segments: list[str] = []
            topic_str = sub["topic"]
            for s in sub.get("segments", []):
                if s == "+":
                    segments.append("CLOUD_WILDCARD_SINGLE")
                elif s == "#":
                    segments.append("CLOUD_WILDCARD_MULTI")
                else:
                    mapped = False
                    if topic_str in valid_topic_names:
                        c_name = "DatastoreAction" if topic_str == "DATASTORE" else f"{topic_str.lower().title()}Action"
                        for act in spec.actions:
                            if act["name"].startswith(f"{topic_str}_") and act["value"] == s:
                                sfx = act["name"].split("_", 1)[1]
                                segments.append(f"{c_name}.{sfx}.value")
                                mapped = True
                                break
                    if not mapped:
                        segments.append(f'"{s}"')

            subscriptions.append(
                {
                    "topic": topic_str,
                    "qos": sub["qos"],
                    "segments_tuple": f"({', '.join(segments)},)" if segments else "()",
                }
            )
        return subscriptions

    def generate_python(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("protocol.py.j2")

        constants = self._extract_python_constants(spec)
        handshake_constants = self._extract_python_handshake_constants(spec)
        grouped_actions = self._group_actions(spec)
        subscriptions = self._process_python_subscriptions(spec)

        # Build command_to_pb mapping reflexively
        command_to_pb: list[tuple[str, str]] = []
        for cmd in spec.commands:
            class_name = cmd_name_to_pb_class(cmd.name)
            if hasattr(spec.pb_module, class_name):
                command_to_pb.append((cmd.name, class_name))

        render = template.render(
            constants=constants,
            handshake_constants=handshake_constants,
            capabilities=spec.capabilities,
            architectures=spec.architectures,
            architecture_display_names=spec.architecture_display_names,
            data_formats=spec.data_formats,
            cloud_suffixes=spec.cloud_suffixes,
            cloud_defaults=spec.cloud_defaults,
            status_reasons=spec.status_reasons,
            statuses=spec.statuses,
            commands=spec.commands,
            ack_commands=[c for c in spec.commands if c.requires_ack],
            response_only_commands=[c for c in spec.commands if c.expects_direct_response],
            topics=spec.topics,
            grouped_actions=grouped_actions,
            subscriptions=subscriptions,
            request_response_pairs=self._build_req_resp_map(spec),
            response_to_req_map=self._build_resp_to_req_map(spec),
            command_to_pb=command_to_pb,
            message_topics=spec.message_topics,
            payload_fields=self._extract_payload_fields(spec),
            topic_auth_map=self._extract_topic_auth_map(spec),
        )
        out_path.write_text(render, encoding="utf-8")

    @staticmethod
    def _extract_payload_fields(spec: ProtocolSpec) -> list[dict[str, str]]:
        envelope_desc = spec.pb_module.DESCRIPTOR.message_types_by_name.get("RpcEnvelope")
        payload_fields: list[dict[str, str]] = []
        if envelope_desc and "payload_type" in envelope_desc.oneofs_by_name:
            oneof_desc = envelope_desc.oneofs_by_name["payload_type"]
            for field_desc in oneof_desc.fields:
                if field_desc.message_type:
                    payload_fields.append({"name": field_desc.message_type.name, "field": field_desc.name})
        return payload_fields

    @staticmethod
    def _extract_topic_auth_map(spec: ProtocolSpec) -> dict[tuple[str, str], str]:
        topic_prefix_map = {
            "analog": "a",
            "digital": "d",
            "shell": "sh",
        }
        auth_desc = getattr(spec.pb_module, "TopicAuthorization", None)
        if not auth_desc:
            return {}
        auth_map: dict[tuple[str, str], str] = {}
        for f in auth_desc.DESCRIPTOR.fields:
            name = f.name
            if name == "console_input":
                auth_map[("console", "in")] = name
                continue
            for long_name, short_val in topic_prefix_map.items():
                if name.startswith(long_name + "_"):
                    auth_map[(short_val, name[len(long_name) + 1 :])] = name
                    break
            else:
                parts = name.split("_", 1)
                if len(parts) == 2:
                    auth_map[(parts[0], parts[1])] = name
        return auth_map

    @staticmethod
    def _build_req_resp_map(spec: ProtocolSpec) -> dict[str, list[str]]:
        pairs: dict[str, list[str]] = {}
        cmd_names = {c.name for c in spec.commands}
        for cmd in spec.commands:
            if cmd.name.endswith("_RESP"):
                req_name = cmd.name[:-5]
                if req_name in cmd_names:
                    pairs.setdefault(req_name, []).append(cmd.name)
        return pairs

    @staticmethod
    def _build_resp_to_req_map(spec: ProtocolSpec) -> dict[str, str]:
        reverse: dict[str, str] = {}
        cmd_names = {c.name for c in spec.commands}
        for cmd in spec.commands:
            if cmd.name.endswith("_RESP"):
                req_name = cmd.name[:-5]
                if req_name in cmd_names:
                    reverse[cmd.name] = req_name
        return reverse

    def generate_nanopb(self, proto_path: Path) -> None:
        """Invoke nanopb_generator.py to create C++ headers/sources."""
        nanopb = importlib.import_module("nanopb")
        nanopb_file = nanopb.__file__
        assert nanopb_file is not None
        nanopb_include_path = Path(nanopb_file).parent / "generator" / "proto"

        cmd = [
            sys.executable,
            "-m",
            "nanopb.generator.nanopb_generator",
            "-v",
            "-I",
            str(proto_path.parent),
            "-I",
            str(nanopb_include_path),
            "-I",
            "/usr/local/include",
            "-I",
            "/usr/include",
            proto_path.name,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=str(proto_path.parent))
        except subprocess.CalledProcessError as e:
            sys.stderr.write(f"Error: nanopb_generator failed: {e.stderr}\n")
            sys.exit(1)

    def generate_python_pb2(self, proto_path: Path, out_dir: Path) -> None:
        """Invoke protoc to generate Python pb2 module and typing stub."""
        # Create a temporary wrapper script for protoc-gen-pyi
        wrapper_path = REPO_ROOT / ".tmp_protoc_plugin.sh"

        wrapper_path.write_text(
            f'#!/bin/bash\n{sys.executable} -c "from mypy_protobuf.main import main; main()" "$@"\n'
        )
        wrapper_path.chmod(0o755)

        nanopb = importlib.import_module("nanopb")
        nanopb_file = nanopb.__file__
        assert nanopb_file is not None
        nanopb_include_path = Path(nanopb_file).parent / "generator" / "proto"

        env = os.environ.copy()
        # Ensure the user's local site-packages are in the path for the wrapper
        user_site = site.getusersitepackages()
        env["PYTHONPATH"] = f"{user_site}:{env.get('PYTHONPATH', '')}"

        cmd = [
            str(protoc_bin),
            f"--python_out={out_dir}",
            f"--pyi_out={out_dir}",
            f"--grpclib_python_out={out_dir}",
            f"--plugin=protoc-gen-pyi={wrapper_path}",
            f"--proto_path={proto_path.parent}",
            f"--proto_path={nanopb_include_path}",
            "--proto_path=/usr/local/include",
            "--proto_path=/usr/include",
            str(proto_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
        except subprocess.CalledProcessError as e:
            sys.stderr.write(f"Error: protoc failed: {e.stderr}\n")
            sys.exit(1)
        finally:
            if wrapper_path.exists():
                wrapper_path.unlink()

    def generate_python_client(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("protocol_client.py.j2")

        constants: list[dict[str, Any]] = []
        for field in spec.constants_opt.DESCRIPTOR.fields:
            opts = field.GetOptions()
            if opts.Extensions[spec.pb_module.client_constant]:
                py_name = opts.Extensions[spec.pb_module.py_name]
                val = getattr(spec.constants_opt, field.name)
                constants.append({"name": py_name, "type": "int", "value": val})

        render = template.render(
            constants=constants,
            capabilities=spec.capabilities,
            statuses=spec.statuses,
            commands=spec.commands,
            topics=spec.topics,
        )
        out_path.write_text(render, encoding="utf-8")


def update_metadata(version: str) -> None:
    targets = [
        (REPO_ROOT / "pyproject.toml", r'version\s*=\s*"[^"]+"', f'version = "{version}"', 1),
        (REPO_ROOT / "mcubridge" / "Makefile", r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={version}", 0),
        (REPO_ROOT / "mcubridge-library-arduino" / "library.properties", r"version=[^\n]+", f"version={version}", 0),
    ]
    for target_path, pattern, repl, count in targets:
        if target_path.exists():
            content = target_path.read_text(encoding="utf-8")
            updated = re.sub(pattern, repl, content, count=count)
            target_path.write_text(updated, encoding="utf-8")
            sys.stderr.write(f"Updated {target_path} to version {version}\n")


def _format_python_file(path: Path) -> None:
    """Post-process a generated Python file with black for canonical formatting. [SIL-2]"""
    try:
        res = subprocess.run(
            [sys.executable, "-m", "black", "--quiet", str(path)],
            check=False,
            capture_output=True,
        )
        if res.returncode != 0:
            res_fallback = subprocess.run(
                ["black", "--quiet", str(path)],
                check=False,
                capture_output=True,
            )
            if res_fallback.returncode != 0:
                raise RuntimeError(
                    f"black is mandatory and failed to format {path}. "
                    f"Ensure black is installed: {res_fallback.stderr.decode()}"
                )
    except FileNotFoundError as err:
        raise RuntimeError(
            f"black is mandatory and was not found in PATH or Python environment ({sys.executable}). "
            f"Please install black (`pip install black` / `apt install python3-black`)."
        ) from err


def ensure_nanopb_core_files() -> None:
    """Ensure the core Nanopb C files exist in mcubridge-library-arduino/src/."""

    src_dir = REPO_ROOT / "mcubridge-library-arduino" / "src"
    version = "nanopb-0.4.9.1"
    base_url = f"https://raw.githubusercontent.com/nanopb/nanopb/{version}/"
    files = [
        "pb.h",
        "pb_common.h",
        "pb_common.c",
        "pb_decode.h",
        "pb_decode.c",
        "pb_encode.h",
        "pb_encode.c",
    ]

    src_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        target = src_dir / f
        if not target.exists():
            url = base_url + f
            sys.stderr.write(f"Downloading core Nanopb file: {f} from {url}...\n")
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    target.write_bytes(response.read())
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
                sys.stderr.write(f"Error downloading {f}: {e}\n")
                sys.exit(1)


def check_incremental_build(args: Any, version: str) -> tuple[bool, Path, str]:
    proto_path = args.spec.resolve()
    h = hashlib.sha256()
    h.update(proto_path.read_bytes())
    h.update(version.encode("utf-8"))
    templates_dir = Path(__file__).resolve().parent / "templates"
    if templates_dir.exists():
        for t_file in sorted(templates_dir.glob("*.j2")):
            h.update(t_file.read_bytes())
    current_hash = h.hexdigest()

    hash_file = proto_path.parent / ".mcubridge.proto.hash"

    # Check if all output files exist
    outputs_exist = True
    for out in [args.cpp, args.cpp_structs, args.py, args.py_client]:
        if out and not out.exists():
            outputs_exist = False
            break

    # Also check if mcubridge_pb2.py and untyped_libs type stubs exist in target locations
    if outputs_exist:
        if args.py and not (args.py.parent / "mcubridge_pb2.py").exists():
            outputs_exist = False
        if args.py_client and not (args.py_client.parent / "mcubridge_pb2.py").exists():
            outputs_exist = False
        for lib in UNTYPED_LIBS:
            if not (REPO_ROOT / "typings" / lib).exists() and not (REPO_ROOT / "typings" / "stubs" / lib).exists():
                outputs_exist = False
                break

    up_to_date = bool(outputs_exist and hash_file.exists() and hash_file.read_text().strip() == current_hash)
    return up_to_date, hash_file, current_hash


def _copy_generated_python_files(proto_path: Path, args: Any) -> None:
    py_pb2 = proto_path.parent / "mcubridge_pb2.py"
    py_pb2_stub = proto_path.parent / "mcubridge_pb2.pyi"
    if py_pb2.exists():
        pb2_data = py_pb2.read_bytes()
        if args.py:
            (args.py.parent / "mcubridge_pb2.py").write_bytes(pb2_data)
        if args.py_client:
            (args.py_client.parent / "mcubridge_pb2.py").write_bytes(pb2_data)
        py_pb2.unlink(missing_ok=True)
    if py_pb2_stub.exists():
        pb2_stub_text = py_pb2_stub.read_text()
        pb2_stub_text = pb2_stub_text.replace(
            "_Union[StructuredEntry, _Mapping]]",
            "_Union[StructuredEntry, _Mapping[str, object]]]",
        )
        pb2_stub_data = pb2_stub_text.encode()
        if args.py:
            (args.py.parent / "mcubridge_pb2.pyi").write_bytes(pb2_stub_data)
        if args.py_client:
            (args.py_client.parent / "mcubridge_pb2.pyi").write_bytes(pb2_stub_data)
        py_pb2_stub.unlink(missing_ok=True)

    py_grpc = proto_path.parent / "mcubridge_grpc.py"
    if py_grpc.exists():
        grpc_text = py_grpc.read_text()
        grpc_text = grpc_text.replace("import mcubridge_pb2", "from . import mcubridge_pb2")
        grpc_data = grpc_text.encode()
        if args.py:
            (args.py.parent / "mcubridge_grpc.py").write_bytes(grpc_data)
        if args.py_client:
            (args.py_client.parent / "mcubridge_grpc.py").write_bytes(grpc_data)
        py_grpc.unlink(missing_ok=True)


cli = typer.Typer(help="Protocol binding generator for MCU Bridge v2.", add_completion=False)


@cli.command()
def main(
    spec_file: Annotated[Path, typer.Option("--spec", help="Protocol specification file (.proto)")],
    cpp: Annotated[Path | None, typer.Option("--cpp", help="C++ header output")] = None,
    cpp_structs: Annotated[Path | None, typer.Option("--cpp-structs", help="C++ structs output")] = None,
    py: Annotated[Path | None, typer.Option("--py", help="Python output")] = None,
    py_client: Annotated[Path | None, typer.Option("--py-client", help="Python client output")] = None,
) -> None:
    ensure_nanopb_core_files()

    @dataclass
    class Args:
        spec: Path
        cpp: Path | None
        cpp_structs: Path | None
        py: Path | None
        py_client: Path | None

    args = Args(spec=spec_file, cpp=cpp, cpp_structs=cpp_structs, py=py, py_client=py_client)
    gen = JinjaGenerator()
    version = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "0.0.0"
    if version == "0.0.0":
        sys.stderr.write(f"Warning: VERSION file not found at {VERSION_PATH}, using fallback.\n")

    up_to_date, hash_file, current_hash = check_incremental_build(args, version)
    if up_to_date:
        sys.stderr.write("Protocol bindings up-to-date, skipping generation.\n")
        return

    update_metadata(version)

    # Compile the protobuf first to generate mcubridge_pb2.py
    proto_path = args.spec.resolve()
    if proto_path.suffix == ".toml":
        proto_path = (proto_path.parent / "mcubridge.proto").resolve()

    if proto_path.exists():
        sys.stderr.write(f"Compiling {proto_path}...\n")
        # Python PB2
        gen.generate_python_pb2(proto_path, proto_path.parent)
        # Nanopb C++
        gen.generate_nanopb(proto_path)

    # Now load the compiled descriptor
    proto_spec = load_spec_from_proto(proto_path)

    # Move generated files to target locations
    if proto_path.exists():
        if args.cpp:
            cpp_pb_h = proto_path.parent / "mcubridge.pb.h"
            cpp_pb_c = proto_path.parent / "mcubridge.pb.c"
            target_h = args.cpp.parent / "mcubridge.pb.h"
            target_c = args.cpp.parent / "mcubridge.pb.c"

            if cpp_pb_h.exists():
                target_h.write_bytes(cpp_pb_h.read_bytes())
                # Fix pb.h include for relative path in Arduino library structure
                h_text = target_h.read_text().replace("#include <pb.h>", '#include "../pb.h"')
                target_h.write_text(h_text)
                cpp_pb_h.unlink(missing_ok=True)
            if cpp_pb_c.exists():
                target_c.write_bytes(cpp_pb_c.read_bytes())
                cpp_pb_c.unlink(missing_ok=True)

        _copy_generated_python_files(proto_path, args)

    if args.cpp:
        args.cpp.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_header(proto_spec, args.cpp, version)
        sys.stderr.write(f"Generated {args.cpp}\n")

        # Generate hardware config next to the main header
        hw_config_path = args.cpp.parent / "rpc_hw_config.h"
        gen.generate_cpp_hw_config(proto_spec, hw_config_path)
        sys.stderr.write(f"Generated {hw_config_path}\n")

    if args.cpp_structs:
        args.cpp_structs.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_structs(proto_spec, args.cpp_structs)
        sys.stderr.write(f"Generated {args.cpp_structs}\n")

    if args.py:
        args.py.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_python(proto_spec, args.py)
        _format_python_file(args.py)
        sys.stderr.write(f"Generated {args.py}\n")

    if args.py_client:
        args.py_client.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_python_client(proto_spec, args.py_client)
        _format_python_file(args.py_client)
        sys.stderr.write(f"Generated {args.py_client}\n")

    # [SIL-2] Generate type stubs for untyped libraries using pyright if stub
    # directory is missing (auto-installs pyright if needed).
    missing_stubs = [
        lib
        for lib in UNTYPED_LIBS
        if not (REPO_ROOT / "typings" / lib).exists() and not (REPO_ROOT / "typings" / "stubs" / lib).exists()
    ]
    if missing_stubs:
        has_pyright = shutil.which("pyright") is not None or importlib.util.find_spec("pyright") is not None
        if not has_pyright:
            sys.stderr.write("Installing pyright for stub generation...\n")
            subprocess.run([sys.executable, "-m", "pip", "install", "pyright"], check=False, capture_output=True)
            has_pyright = shutil.which("pyright") is not None or importlib.util.find_spec("pyright") is not None

        if has_pyright:
            sys.stderr.write(f"Generating type stubs for {', '.join(missing_stubs)}...\n")
            for lib in missing_stubs:
                try:
                    res = subprocess.run(
                        [sys.executable, "-m", "pyright", "--createstub", lib], check=False, capture_output=True
                    )
                    if res.returncode != 0:
                        subprocess.run(["pyright", "--createstub", lib], check=False, capture_output=True)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    pass

                # [SIL-2] Fix generated prometheus_client core.pyi stub to export necessary types
                if lib == "prometheus_client":
                    core_stub = REPO_ROOT / "typings" / "prometheus_client" / "core.pyi"
                    if core_stub.exists():
                        core_content = (
                            "from .metrics_core import (\n"
                            "    Metric,\n"
                            "    UnknownMetricFamily,\n"
                            "    UntypedMetricFamily,\n"
                            "    CounterMetricFamily,\n"
                            "    GaugeMetricFamily,\n"
                            "    SummaryMetricFamily,\n"
                            "    HistogramMetricFamily,\n"
                            "    GaugeHistogramMetricFamily,\n"
                            "    InfoMetricFamily,\n"
                            "    StateSetMetricFamily,\n"
                            ")\n"
                            "from .metrics import Counter, Enum, Gauge, Histogram, Info, Summary\n"
                            "from .registry import CollectorRegistry, REGISTRY\n"
                            "from .samples import Sample, Exemplar, NativeHistogram, Timestamp\n\n"
                            "__all__ = ('BucketSpan', 'CollectorRegistry', 'Counter', "
                            "'CounterMetricFamily', 'Enum', 'Exemplar', 'Gauge', 'GaugeHistogramMetricFamily', "
                            "'GaugeMetricFamily', 'Histogram', 'HistogramMetricFamily', 'Info', 'InfoMetricFamily', "
                            "'Metric', 'NativeHistogram', 'REGISTRY', 'Sample', 'StateSetMetricFamily', 'Summary', "
                            "'SummaryMetricFamily', 'Timestamp', 'UnknownMetricFamily', 'UntypedMetricFamily')\n"
                        )
                        core_stub.write_text(core_content, encoding="utf-8")

                # [SIL-2] Fix generated typer stubs: pyright's --createstub leaves
                # click.ParamType (a Generic class) unparameterized, which makes
                # every overload of typer.Option/Argument/OptionInfo/ArgumentInfo
                # "partially unknown" under pyright strict (reportUnknownMemberType),
                # even though the call site itself resolves cleanly. Parameterize
                # every bare occurrence with `click.ParamType[Any]` / `click.types.ParamType[Any]`
                # so the stub's declared type is fully known.
                if lib == "typer":
                    for stub_name in ("params.pyi", "models.pyi", "core.pyi", "main.pyi"):
                        typer_stub = REPO_ROOT / "typings" / "typer" / stub_name
                        if typer_stub.exists():
                            typer_content = typer_stub.read_text(encoding="utf-8")
                            typer_content = typer_content.replace(
                                "click.ParamType | None", "click.ParamType[Any] | None"
                            )
                            typer_content = typer_content.replace(
                                "click.types.ParamType | Any | None", "click.types.ParamType[Any] | None"
                            )
                            typer_content = typer_content.replace("-> click.ParamType:", "-> click.ParamType[Any]:")
                            typer_stub.write_text(typer_content, encoding="utf-8")

    # Save hash for incremental compilation
    hash_file.write_text(current_hash, encoding="utf-8")


if __name__ == "__main__":
    cli()
