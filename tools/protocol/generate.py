#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Jinja2: Declarative templates for C++ and Python outputs.

Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, Any, cast
import typer
from jinja2 import Environment, FileSystemLoader  # type: ignore[import-untyped]

app = typer.Typer(help="Protocol binding generator for MCU Bridge v2.")

# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY VALIDATION (CRITICAL)
# ═════════════════════════════════════════════════════════════════════════════
REQUIRED_DEPS = ["jinja2", "google.protobuf", "nanopb"]
MISSING_DEPS: list[str] = []
UNTYPED_LIBS = ["cobs", "prometheus_client", "serialx", "uci", "uvloop", "typer", "lmdb"]

for dep in REQUIRED_DEPS:
    if importlib.util.find_spec(dep.split(".")[0]) is None:
        MISSING_DEPS.append(dep)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Check for protoc binary (check local project bin first)
# Use lowercase to avoid Pyright constant redefinition error
protoc_bin = (REPO_ROOT / "bin" / "protoc").resolve()
if not protoc_bin.exists():
    protoc_bin = Path("protoc")

HAS_PROTOC = subprocess.run([str(protoc_bin), "--version"], capture_output=True, check=False).returncode == 0

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
    if cmd_name.startswith("CMD_"):
        cmd_name = cmd_name[4:]
    segments = cmd_name.split("_")
    mapped_segments: list[str] = []
    for seg in segments:
        if seg == "RESP":
            mapped_segments.append("Response")
        else:
            mapped_segments.append(seg.capitalize())
    return "".join(mapped_segments)


class CommandDef:
    def __init__(
        self,
        name: str,
        value: int,
        directions: list[str],
        category: str | None = None,
        description: str | None = None,
        requires_ack: bool = False,
        expects_direct_response: bool = False,
        cloud_topic: str | None = None,
    ) -> None:
        self.name = name
        self.value = value
        self.directions = directions
        self.category = category
        self.description = description
        self.requires_ack = requires_ack
        self.expects_direct_response = expects_direct_response
        self.cloud_topic = cloud_topic


class StatusDef:
    def __init__(self, name: str, value: int, description: str) -> None:
        self.name = name
        self.value = value
        self.description = description


class ProtocolSpec:
    def __init__(
        self,
        constants: dict[str, Any],
        hardware: dict[str, Any],
        commands: list[CommandDef],
        statuses: list[StatusDef],
        handshake: dict[str, Any],
        cloud_subscriptions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        topics: list[dict[str, Any]],
        capabilities: dict[str, int],
        architectures: dict[str, int],
        data_formats: dict[str, str],
        cloud_suffixes: dict[str, str],
        cloud_defaults: dict[str, str],
        status_reasons: dict[str, str],
        architecture_display_names: dict[str, str],
        message_topics: dict[str, str],
    ) -> None:
        self.constants = constants
        self.hardware = hardware
        self.commands = commands
        self.statuses = statuses
        self.handshake = handshake
        self.cloud_subscriptions = cloud_subscriptions
        self.actions = actions
        self.topics = topics
        self.capabilities = capabilities
        self.architectures = architectures
        self.data_formats = data_formats
        self.cloud_suffixes = cloud_suffixes
        self.cloud_defaults = cloud_defaults
        self.status_reasons = status_reasons
        self.architecture_display_names = architecture_display_names
        self.message_topics = message_topics
        self.constants_opt: Any = None
        self.hardware_opt: Any = None
        self.handshake_opt: Any = None
        self.data_formats_opt: Any = None
        self.pb_module: Any = None


def load_spec_from_proto(proto_path: Path) -> ProtocolSpec:
    import importlib
    from google.protobuf.json_format import MessageToDict

    proto_dir = str(proto_path.parent)
    if proto_dir not in sys.path:
        sys.path.insert(0, proto_dir)

    if "mcubridge_pb2" in sys.modules:
        del sys.modules["mcubridge_pb2"]

    # Pre-register local buf.validate package to prevent collision with
    # any system 'buf' package (e.g. on Python 3.14+ / Ubuntu 26.04)
    buf_dir = proto_path.parent / "buf"
    buf_validate_dir = buf_dir / "validate"
    if buf_validate_dir.is_dir() and "buf" not in sys.modules:
        import types

        buf_mod = types.ModuleType("buf")
        buf_mod.__path__ = [str(buf_dir)]
        sys.modules["buf"] = buf_mod

        buf_validate_mod = types.ModuleType("buf.validate")
        buf_validate_mod.__path__ = [str(buf_validate_dir)]
        sys.modules["buf.validate"] = buf_validate_mod

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
    cloud_defaults = MessageToDict(
        cloud_defaults_opt, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )
    status_reasons = MessageToDict(
        status_reasons_opt, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )

    cloud_subscriptions = [
        MessageToDict(sub, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
        for sub in cloud_subscriptions_opt
    ]
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
        self.env: Any = cast(Any, Environment)(
            loader=cast(Any, FileSystemLoader)(str(TEMPLATE_DIR)),
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

        v_major, v_minor, v_patch = map(int, version.split("."))

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
        proto_path = (REPO_ROOT / "tools" / "protocol" / "mcubridge.proto").resolve()
        proto_content = proto_path.read_text(encoding="utf-8")

        # 1. Extract ALL messages for basic aliases and get_fields
        all_msg_names = re.findall(r"(?:^|\n)\s*message\s+(\w+)\s*{", proto_content)
        options_path = (REPO_ROOT / "tools" / "protocol" / "mcubridge.options").resolve()
        options_content = options_path.read_text(encoding="utf-8")
        skipped_messages = re.findall(r"rpc\.pb\.(\w+)\s+skip_message:true", options_content)

        all_structs = [
            {"name": name} for name in all_msg_names if name not in skipped_messages and name != "RpcContainer"
        ]

        # 2. Extract messages inside RpcEnvelope oneof for payload helpers
        oneof_match = re.search(r"oneof payload_type\s*{(.*?)}", proto_content, re.DOTALL)
        payload_structs: list[dict[str, str]] = []
        if oneof_match:
            oneof_content = oneof_match.group(1)
            for raw_line in oneof_content.strip().split("\n"):
                line = raw_line.strip()
                if not line or line.startswith("//"):
                    continue
                m = re.search(r"(\w+)\s+(\w+)\s*=\s*(\d+);", line)
                if m:
                    msg_type, field_name, _ = m.groups()
                    if msg_type == "bytes":
                        continue
                    if msg_type not in skipped_messages:
                        payload_structs.append({"name": msg_type, "field": field_name})

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
        )
        out_path.write_text(render, encoding="utf-8")

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
        import importlib

        nanopb = importlib.import_module("nanopb")
        nanopb_file = nanopb.__file__
        assert nanopb_file is not None
        nanopb_include_path = Path(nanopb_file).parent / "generator" / "proto"

        v_proto = proto_path.parent / "buf" / "validate" / "validate.proto"
        if v_proto.exists():
            v_cmd = [
                sys.executable,
                "-m",
                "nanopb.generator.nanopb_generator",
                "-v",
                "-I",
                str(proto_path.parent),
                "-I",
                str(nanopb_include_path),
                "buf/validate/validate.proto",
            ]
            subprocess.run(v_cmd, check=False, capture_output=True, text=True, cwd=str(proto_path.parent))

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

        import os
        import site
        import importlib

        nanopb = importlib.import_module("nanopb")
        nanopb_file = nanopb.__file__
        assert nanopb_file is not None
        nanopb_include_path = Path(nanopb_file).parent / "generator" / "proto"

        env = os.environ.copy()
        # Ensure the user's local site-packages are in the path for the wrapper
        user_site = site.getusersitepackages()
        env["PYTHONPATH"] = f"{user_site}:{env.get('PYTHONPATH', '')}"

        validate_proto = proto_path.parent / "buf" / "validate" / "validate.proto"
        if validate_proto.exists():
            v_cmd = [
                str(protoc_bin),
                f"--python_out={out_dir}",
                f"--pyi_out={out_dir}",
                f"--plugin=protoc-gen-pyi={wrapper_path}",
                f"--proto_path={proto_path.parent}",
                str(validate_proto),
            ]
            subprocess.run(v_cmd, check=False, capture_output=True, env=env)

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


def update_metadata(version: str):
    # 1. pyproject.toml
    pyproj = REPO_ROOT / "pyproject.toml"
    if pyproj.exists():
        content = pyproj.read_text(encoding="utf-8")
        content = re.sub(r'version\s*=\s*"[^"]+"', f'version = "{version}"', content, count=1)
        pyproj.write_text(content, encoding="utf-8")
        sys.stderr.write(f"Updated {pyproj} to version {version}\n")

    # 2. mcubridge/Makefile
    makefile = REPO_ROOT / "mcubridge" / "Makefile"
    if makefile.exists():
        content = makefile.read_text(encoding="utf-8")
        content = re.sub(r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={version}", content)
        makefile.write_text(content, encoding="utf-8")
        sys.stderr.write(f"Updated {makefile} to version {version}\n")

    # 3. mcubridge-library-arduino/library.properties
    lib_prop = REPO_ROOT / "mcubridge-library-arduino" / "library.properties"
    if lib_prop.exists():
        content = lib_prop.read_text(encoding="utf-8")
        content = re.sub(r"version=[^\n]+", f"version={version}", content)
        lib_prop.write_text(content, encoding="utf-8")
        sys.stderr.write(f"Updated {lib_prop} to version {version}\n")


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


def ensure_protovalidate_proto_files() -> None:
    """Ensure the vendored buf/validate/validate.proto exists.

    This is the upstream schema for protovalidate's `(buf.validate.field)` /
    `(buf.validate.message)` annotations. It ships with the bufbuild/protovalidate
    project (not with the protovalidate PyPI wheel, which expects consumers to
    generate/vendor it themselves), so it is downloaded on demand from the
    pinned release tag and verified against a pinned hash, exactly like
    ensure_nanopb_core_files() does for Nanopb. Kept in sync with the version
    pinned in feeds/python3-protovalidate/Makefile.
    """

    target = REPO_ROOT / "tools" / "protocol" / "buf" / "validate" / "validate.proto"
    if target.exists():
        return

    version = "v1.2.0"
    expected_sha256 = "817951eab518442950f1982be24b2c3154b214b40e42f49b915cbf5181e6ee80"
    url = f"https://raw.githubusercontent.com/bufbuild/protovalidate/{version}/proto/protovalidate/buf/validate/validate.proto"

    target.parent.mkdir(parents=True, exist_ok=True)
    sys.stderr.write(f"Downloading vendored protovalidate schema from {url}...\n")
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            data = response.read()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        sys.stderr.write(f"Error downloading {url}: {e}\n")
        sys.exit(1)

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        sys.stderr.write(
            "Hash mismatch for vendored validate.proto: " f"expected {expected_sha256}, got {actual_sha256}\n"
        )
        sys.exit(1)

    target.write_bytes(data)


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
        if args.py and not (args.py.parent.parent.parent / "buf" / "validate" / "validate_pb2.pyi").exists():
            outputs_exist = False
        if args.py_client and not (args.py_client.parent / "buf" / "validate" / "validate_pb2.pyi").exists():
            outputs_exist = False
        for lib in UNTYPED_LIBS:
            if not (REPO_ROOT / "typings" / lib).exists():
                outputs_exist = False
                break

    up_to_date = bool(outputs_exist and hash_file.exists() and hash_file.read_text().strip() == current_hash)
    return up_to_date, hash_file, current_hash


def _copy_generated_python_files(proto_path: Path, args: Any) -> None:
    # The `mcubridge` package (daemon side) calls protovalidate.validate() at
    # runtime, and protovalidate's own internal validator hard-imports the
    # absolute top-level module `buf.validate.validate_pb2` (it does not ship
    # this generated stub itself). To share a single FileDescriptor
    # registration for "buf/validate/validate.proto" between our generated
    # mcubridge_pb2.py and protovalidate, `buf` must be installed as a
    # genuine top-level package (declared in pyproject.toml's `packages`),
    # not nested inside mcubridge.protocol. The client library never calls
    # protovalidate directly, so it keeps a self-contained, relatively
    # imported nested copy instead.
    v_pb2 = proto_path.parent / "buf" / "validate" / "validate_pb2.py"
    v_pb2_stub = proto_path.parent / "buf" / "validate" / "validate_pb2.pyi"
    if v_pb2.exists():
        v_data = v_pb2.read_bytes()
        v_stub_data = v_pb2_stub.read_bytes() if v_pb2_stub.exists() else None
        if args.py:
            v_dir = args.py.parent.parent.parent / "buf" / "validate"
            v_dir.mkdir(parents=True, exist_ok=True)
            (v_dir / "validate_pb2.py").write_bytes(v_data)
            (v_dir / "__init__.py").write_bytes(b"")
            (args.py.parent.parent.parent / "buf" / "__init__.py").write_bytes(b"")
            if v_stub_data is not None:
                (v_dir / "validate_pb2.pyi").write_bytes(v_stub_data)
        if args.py_client:
            vc_dir = args.py_client.parent / "buf" / "validate"
            vc_dir.mkdir(parents=True, exist_ok=True)
            (vc_dir / "validate_pb2.py").write_bytes(v_data)
            (vc_dir / "__init__.py").write_bytes(b"")
            (args.py_client.parent / "buf" / "__init__.py").write_bytes(b"")
            if v_stub_data is not None:
                (vc_dir / "validate_pb2.pyi").write_bytes(v_stub_data)

    py_pb2 = proto_path.parent / "mcubridge_pb2.py"
    py_pb2_stub = proto_path.parent / "mcubridge_pb2.pyi"
    if py_pb2.exists():
        pb2_text = py_pb2.read_text()
        pb2_text_client = pb2_text.replace(
            "from buf.validate import validate_pb2", "from .buf.validate import validate_pb2"
        )
        pb2_data = pb2_text.encode()
        pb2_data_client = pb2_text_client.encode()
        if args.py:
            (args.py.parent / "mcubridge_pb2.py").write_bytes(pb2_data)
        if args.py_client:
            (args.py_client.parent / "mcubridge_pb2.py").write_bytes(pb2_data_client)
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
        grpc_text_client = grpc_text.replace(
            "import buf.validate.validate_pb2", "from .buf.validate import validate_pb2"
        )
        grpc_data = grpc_text.encode()
        grpc_data_client = grpc_text_client.encode()
        if args.py:
            (args.py.parent / "mcubridge_grpc.py").write_bytes(grpc_data)
        if args.py_client:
            (args.py_client.parent / "mcubridge_grpc.py").write_bytes(grpc_data_client)
        py_grpc.unlink(missing_ok=True)


@app.command()
def main(
    spec: Annotated[Path, typer.Option("--spec", help="Protocol specification file (.proto)")] = Path(
        "tools/protocol/mcubridge.proto"
    ),
    cpp: Annotated[Path | None, typer.Option("--cpp", help="C++ header output")] = None,
    cpp_structs: Annotated[Path | None, typer.Option("--cpp-structs", help="C++ structs output")] = None,
    py: Annotated[Path | None, typer.Option("--py", help="Python output")] = None,
    py_client: Annotated[Path | None, typer.Option("--py-client", help="Python client output")] = None,
) -> None:
    ensure_nanopb_core_files()
    ensure_protovalidate_proto_files()

    class ArgsObj:
        def __init__(
            self,
            spec_val: Path,
            cpp_val: Path | None,
            cpp_structs_val: Path | None,
            py_val: Path | None,
            py_client_val: Path | None,
        ) -> None:
            self.spec = spec_val
            self.cpp = cpp_val
            self.cpp_structs = cpp_structs_val
            self.py = py_val
            self.py_client = py_client_val

    args = ArgsObj(spec, cpp, cpp_structs, py, py_client)
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
    spec_data = load_spec_from_proto(proto_path)

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
                h_text = h_text.replace(
                    '#include "buf/validate/validate.pb.h"', '/* #include "buf/validate/validate.pb.h" */'
                )
                target_h.write_text(h_text)
                cpp_pb_h.unlink(missing_ok=True)
            if cpp_pb_c.exists():
                target_c.write_bytes(cpp_pb_c.read_bytes())
                cpp_pb_c.unlink(missing_ok=True)

        _copy_generated_python_files(proto_path, args)

    if args.cpp:
        args.cpp.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_header(spec_data, args.cpp, version)
        sys.stderr.write(f"Generated {args.cpp}\n")

        # Generate hardware config next to the main header
        hw_config_path = args.cpp.parent / "rpc_hw_config.h"
        gen.generate_cpp_hw_config(spec_data, hw_config_path)
        sys.stderr.write(f"Generated {hw_config_path}\n")

    if args.cpp_structs:
        args.cpp_structs.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_cpp_structs(spec_data, args.cpp_structs)
        sys.stderr.write(f"Generated {args.cpp_structs}\n")

    if args.py:
        args.py.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_python(spec_data, args.py)
        _format_python_file(args.py)
        sys.stderr.write(f"Generated {args.py}\n")

    if args.py_client:
        args.py_client.parent.mkdir(parents=True, exist_ok=True)
        gen.generate_python_client(spec_data, args.py_client)
        _format_python_file(args.py_client)
        sys.stderr.write(f"Generated {args.py_client}\n")

    # [SIL-2] Generate type stubs for untyped libraries using pyright only if stub directory is missing.
    missing_stubs = [lib for lib in UNTYPED_LIBS if not (REPO_ROOT / "typings" / lib).exists()]
    if missing_stubs:
        sys.stderr.write(f"Generating type stubs for {', '.join(missing_stubs)}...\n")
        for lib in missing_stubs:
            try:
                res = subprocess.run(
                    [sys.executable, "-m", "pyright", "--createstub", lib], check=False, capture_output=True
                )
                if res.returncode != 0:
                    subprocess.run(["pyright", "--createstub", lib], check=False, capture_output=True)
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                sys.stderr.write(f"Warning: Failed to generate stubs for {lib}: {e}\n")

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
                        typer_content = typer_content.replace("click.ParamType | None", "click.ParamType[Any] | None")
                        typer_content = typer_content.replace(
                            "click.types.ParamType | Any | None", "click.types.ParamType[Any] | None"
                        )
                        typer_content = typer_content.replace("-> click.ParamType:", "-> click.ParamType[Any]:")
                        typer_stub.write_text(typer_content, encoding="utf-8")

    # Save hash for incremental compilation
    hash_file.write_text(current_hash, encoding="utf-8")


if __name__ == "__main__":
    main()
