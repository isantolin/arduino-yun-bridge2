#!/usr/bin/env python3
"""Protocol binding generator for MCU Bridge v2.

Architecture:
- Model: Strongly typed dataclasses representing the protocol spec.
- Jinja2: Declarative templates for C++ and Python outputs.

Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import argparse
from jinja2 import Environment, FileSystemLoader

# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY VALIDATION (CRITICAL)
# ═════════════════════════════════════════════════════════════════════════════
REQUIRED_DEPS = ["msgspec", "jinja2", "google.protobuf", "nanopb"]
MISSING_DEPS: list[str] = []

for dep in REQUIRED_DEPS:
    if importlib.util.find_spec(dep.split(".")[0]) is None:
        MISSING_DEPS.append(dep)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Check for protoc binary (check local project bin first)
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

# Load ProtocolSpec directly from spec_model.py via importlib.util
if TYPE_CHECKING:
    from mcubridge.protocol.spec_model import ProtocolSpec
else:
    _SPEC_MODEL_PATH = REPO_ROOT / "mcubridge" / "mcubridge" / "protocol" / "spec_model.py"  # noqa: W503
    _loader_spec = importlib.util.spec_from_file_location("spec_model", str(_SPEC_MODEL_PATH))
    assert _loader_spec is not None and _loader_spec.loader is not None
    _spec_mod = importlib.util.module_from_spec(_loader_spec)
    _loader_spec.loader.exec_module(_spec_mod)
    ProtocolSpec = _spec_mod.ProtocolSpec

TEMPLATE_DIR = Path(__file__).parent / "templates"
VERSION_PATH = REPO_ROOT / "VERSION"


def packet_class_name(proto_name: str) -> str:
    """Convert proto message name to Python Packet class name."""
    if proto_name.endswith("Packet"):
        return proto_name
    return f"{proto_name}Packet"


class JinjaGenerator:
    def __init__(self) -> None:
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            keep_trailing_newline=True,
        )
        self.env.filters["cpp_digits"] = self._cpp_digit_separator

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

    def _extract_proto_metadata(self, pb2_path: Path) -> dict[str, Any]:
        """Extract Enums and Message definitions from the generated pb2 module."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("proto_metadata", str(pb2_path))
        if not spec or not spec.loader:
            return {}
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        metadata = {"commands": [], "statuses": [], "messages": []}

        # Extract Enums
        for enum_name in ["Command", "Status"]:
            if hasattr(mod, enum_name):
                enum_desc = getattr(mod, enum_name)
                for name, val in enum_desc.items():
                    if name in ["CMD_INVALID", "STATUS_INVALID"]:
                        continue
                    item = {"name": name, "value": val}
                    if enum_name == "Command":
                        metadata["commands"].append(item)
                    else:
                        metadata["statuses"].append(item)

        # Extract Message names
        for name, obj in mod.__dict__.items():
            if isinstance(obj, type) and hasattr(obj, "DESCRIPTOR"):
                if name in ["RpcEnvelope", "StructuredEntry", "StructuredPayload"]:
                    continue
                metadata["messages"].append({"name": name})

        return metadata

    def generate_cpp_header(self, spec: ProtocolSpec, out_path: Path, version: str) -> None:
        template = self.env.get_template("rpc_protocol.h.j2")

        v_major, v_minor, v_patch = map(int, version.split("."))

        # Convert topics to list of dicts
        topics_data = [{"name": t["name"], "value": t["value"]} for t in spec.topics]

        # Prepare handshake data
        hs = spec.handshake
        handshake_data = {
            "nonce_length": hs["nonce_length"],
            "tag_length": hs["tag_length"],
            "hkdf_output_length": hs["hkdf_output_length"],
            "hkdf_info_session_bytes": ", ".join([f"0x{b:02X}" for b in hs["hkdf_info_session"].encode("ascii")]),
            "hkdf_info_session_len": len(hs["hkdf_info_session"]),
            "hkdf_info_auth_bytes": ", ".join([f"0x{b:02X}" for b in hs["hkdf_info_auth"].encode("ascii")]),
            "hkdf_info_auth_len": len(hs["hkdf_info_auth"]),
            "hkdf_salt_bytes": ", ".join([f"0x{b:02X}" for b in hs["hkdf_salt"].encode("ascii")]),
            "hkdf_salt_len": len(hs["hkdf_salt"]),
            "response_timeout_max_ms": hs["response_timeout_max_ms"],
        }
        
        handshake_constants = []
        for k, v in hs.items():
            if isinstance(v, int):
                name = f"HANDSHAKE_{k.upper()}"
                handshake_constants.append({"name": name, "type": "uint32_t" if v > 65535 else "uint16_t" if v > 255 else "uint8_t", "value": v})

        render = template.render(
            v_major=v_major,
            v_minor=v_minor,
            v_patch=v_patch,
            constants=self._get_constants(spec),
            handshake_constants=handshake_constants,
            handshake=handshake_data,
            capabilities=spec.capabilities,
            architectures=spec.architectures,
            compression=spec.compression,
            statuses=spec.statuses,
            commands=spec.commands,
            ack_commands=[c for c in spec.commands if c.requires_ack],
            status_reasons=spec.status_reasons,
            topics=topics_data,
        )
        out_path.write_text(render, encoding="utf-8")

    def _get_constants(self, spec: ProtocolSpec) -> list[dict[str, Any]]:
        c = spec.constants
        return [
            {"name": "AEAD_NONCE_SIZE", "type": "uint8_t", "value": c["aead_nonce_size"]},
            {"name": "AEAD_TAG_SIZE", "type": "uint8_t", "value": c["aead_tag_size"]},
            {"name": "AEAD_KEY_SIZE", "type": "uint8_t", "value": c["aead_key_size"]},
            {"name": "CRC_SIZE", "type": "uint8_t", "value": c["crc_size"]},
            {"name": "DEFAULT_BAUDRATE", "type": "uint32_t", "value": c["default_baudrate"]},
            {"name": "PROTOCOL_VERSION", "type": "uint8_t", "value": c["protocol_version"]},
            {"name": "MAX_PAYLOAD_SIZE", "type": "uint8_t", "value": c["max_payload_size"]},
            {"name": "DEFAULT_RETRY_LIMIT", "type": "uint8_t", "value": c["default_retry_limit"]},
            {"name": "DEFAULT_ACK_TIMEOUT_MS", "type": "uint16_t", "value": c["default_ack_timeout_ms"]},
            {"name": "BOOTLOADER_MAGIC", "type": "uint32_t", "value": hex(c["bootloader_magic"])},
            {"name": "STATUS_CODE_MIN", "type": "uint16_t", "value": c["status_code_min"]},
            {"name": "STATUS_CODE_MAX", "type": "uint16_t", "value": c["status_code_max"]},
            {"name": "SYSTEM_COMMAND_MIN", "type": "uint16_t", "value": c["system_command_min"]},
            {"name": "SYSTEM_COMMAND_MAX", "type": "uint16_t", "value": c["system_command_max"]},
            {"name": "MAX_COMMAND_ID", "type": "uint16_t", "value": c["max_command_id"]},
            {"name": "MAX_FILEPATH_LENGTH", "type": "uint8_t", "value": c["max_filepath_length"]},
            {"name": "MAX_DATASTORE_KEY_LENGTH", "type": "uint8_t", "value": c["max_datastore_key_length"]},
            {"name": "CMD_FLAG_COMPRESSED", "type": "uint16_t", "value": c["cmd_flag_compressed"]},
            {"name": "UINT8_MASK", "type": "uint8_t", "value": c["uint8_mask"]},
            {"name": "FRAME_DELIMITER", "type": "uint8_t", "value": c["frame_delimiter"]},
            {"name": "RPC_SHA256_DIGEST_SIZE", "type": "uint8_t", "value": 32},
            {"name": "RPC_SHA256_KAT_BUFFER_SIZE", "type": "uint8_t", "value": 64},
            {"name": "RLE_ESCAPE_BYTE", "type": "uint8_t", "value": c["rle_escape_byte"]},
            {"name": "RLE_MIN_RUN_LENGTH", "type": "uint8_t", "value": c["rle_min_run_length"]},
            {"name": "RLE_MAX_RUN_LENGTH", "type": "uint16_t", "value": c["rle_max_run_length"]},
            {"name": "RLE_SINGLE_ESCAPE_MARKER", "type": "uint8_t", "value": c["rle_single_escape_marker"]},
            {"name": "RLE_OFFSET", "type": "uint8_t", "value": c["rle_offset"]},
        ]

    def generate_cpp_hw_config(self, spec: ProtocolSpec, out_path: Path) -> None:
        template = self.env.get_template("rpc_hw_config.h.j2")
        render = template.render(hardware=spec.hardware)
        out_path.write_text(render, encoding="utf-8")

    def generate_python(self, spec: ProtocolSpec, out_path: Path) -> None:
        """Generate Python protocol definitions for the daemon."""
        template = self.env.get_template("protocol.py.j2")
        self._generate_python_common(spec, out_path, template)

    def generate_python_client(self, spec: ProtocolSpec, out_path: Path) -> None:
        """Generate Python protocol definitions for the client (unified)."""
        template = self.env.get_template("protocol.py.j2")
        self._generate_python_common(spec, out_path, template)

    def _generate_python_common(self, spec: ProtocolSpec, out_path: Path, template: Any) -> None:
        c = spec.constants
        constants = [
            {"name": "AEAD_NONCE_SIZE", "type": "int", "value": c["aead_nonce_size"]},
            {"name": "AEAD_TAG_SIZE", "type": "int", "value": c["aead_tag_size"]},
            {"name": "AEAD_KEY_SIZE", "type": "int", "value": c["aead_key_size"]},
            {"name": "FLOW_CONTROL_XOFF_THRESHOLD", "type": "int", "value": spec.hardware["flow_control_xoff_threshold"]},
            {"name": "FLOW_CONTROL_XON_THRESHOLD", "type": "int", "value": spec.hardware["flow_control_xon_threshold"]},
            {"name": "PROTOCOL_VERSION", "type": "int", "value": c["protocol_version"]},
            {"name": "DEFAULT_BAUDRATE", "type": "int", "value": c["default_baudrate"]},
            {"name": "DEFAULT_MQTT_PORT", "type": "int", "value": c["default_mqtt_port"]},
            {"name": "MAX_PAYLOAD_SIZE", "type": "int", "value": c["max_payload_size"]},
            {"name": "BOOTLOADER_MAGIC", "type": "int", "value": c["bootloader_magic"]},
            {"name": "STATUS_CODE_MIN", "type": "int", "value": c["status_code_min"]},
            {"name": "STATUS_CODE_MAX", "type": "int", "value": c["status_code_max"]},
            {"name": "SYSTEM_COMMAND_MIN", "type": "int", "value": c["system_command_min"]},
            {"name": "SYSTEM_COMMAND_MAX", "type": "int", "value": c["system_command_max"]},
            {"name": "DEFAULT_ACK_TIMEOUT_MS", "type": "int", "value": c["default_ack_timeout_ms"]},
            {"name": "DEFAULT_RETRY_LIMIT", "type": "int", "value": c["default_retry_limit"]},
            {"name": "MAX_PENDING_TX_FRAMES", "type": "int", "value": c["max_pending_tx_frames"]},
            {"name": "MAX_COMMAND_ID", "type": "int", "value": c["max_command_id"]},
            {"name": "MAX_FILEPATH_LENGTH", "type": "int", "value": c["max_filepath_length"]},
            {"name": "MAX_DATASTORE_KEY_LENGTH", "type": "int", "value": c["max_datastore_key_length"]},
            {"name": "FILE_LARGE_WARNING_BYTES", "type": "int", "value": spec.hardware["file_large_warning_bytes"]},
            {"name": "CMD_FLAG_COMPRESSED", "type": "int", "value": 0x8000},
            {"name": "UINT16_MAX", "type": "int", "value": 0xFFFF},
        ]

        hs = spec.handshake
        handshake_constants = [{"name": f"HANDSHAKE_{k.upper()}", "value": v} for k, v in hs.items() if isinstance(v, int)]
        handshake_strings = {f"HANDSHAKE_{k.upper()}": v for k, v in hs.items() if isinstance(v, str) and not k.startswith("hkdf_info") and k != "hkdf_salt"}
        handshake_bytes = {f"HANDSHAKE_{k.upper()}": list(v.encode("ascii")) for k, v in hs.items() if isinstance(v, str) and (k.startswith("hkdf_info") or k == "hkdf_salt")}

        # Group actions from spec.actions
        grouped_actions = []
        action_map = {}
        for act in spec.actions:
            if "_" not in act["name"]: continue
            prefix, suffix = act["name"].split("_", 1)
            action_map.setdefault(prefix, []).append({"name": suffix, "value": act["value"]})
        
        for prefix, items in action_map.items():
            cls_name = "DatastoreAction" if prefix == "DATASTORE" else f"{prefix.lower().capitalize()}Action"
            grouped_actions.append({"class_name": cls_name, "action_items": items})

        # Response mappings
        request_response_pairs = {}
        response_to_req_map = {}
        cmd_names = {c.name for c in spec.commands}
        for cmd in spec.commands:
            if cmd.name.endswith("_RESP"):
                req_name = cmd.name[:-5]
                if req_name in cmd_names:
                    request_response_pairs.setdefault(req_name, []).append(cmd.name)
                    response_to_req_map[cmd.name] = req_name

        render = template.render(
            constants=constants,
            handshake_constants=handshake_constants,
            handshake_strings=handshake_strings,
            handshake_bytes=handshake_bytes,
            compression=spec.compression,
            capabilities=spec.capabilities,
            architectures=spec.architectures,
            architecture_display_names=spec.architecture_display_names,
            statuses=spec.statuses,
            status_reasons=spec.status_reasons,
            commands=spec.commands,
            ack_commands=[c for c in spec.commands if c.requires_ack],
            response_only_commands=[c for c in spec.commands if c.expects_direct_response],
            topics=spec.topics,
            grouped_actions=grouped_actions,
            request_response_pairs=request_response_pairs,
            response_to_req_map=response_to_req_map,
            mqtt_defaults=spec.mqtt_defaults,
        )
        out_path.write_text(render, encoding="utf-8")

    def generate_nanopb(self, proto_path: Path) -> None:
        """Invoke nanopb_generator.py to create C++ headers/sources."""
        cmd = [sys.executable, "-m", "nanopb.generator.nanopb_generator", "-v", proto_path.name]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=str(proto_path.parent))
        except subprocess.CalledProcessError as e:
            sys.stderr.write(f"Error: nanopb_generator failed: {e.stderr}\n")
            sys.exit(1)

    def generate_python_pb2(self, proto_path: Path, out_dir: Path) -> None:
        """Invoke protoc to generate Python pb2 module and typing stub."""
        wrapper_path = REPO_ROOT / ".tmp_protoc_plugin.sh"
        wrapper_path.write_text(f'#!/bin/bash\n{sys.executable} -c "from mypy_protobuf.main import main; main()" "$@"\n')
        wrapper_path.chmod(0o755)

        import os, site
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{site.getusersitepackages()}:{env.get('PYTHONPATH', '')}"

        cmd = [str(protoc_bin), f"--python_out={out_dir}", f"--pyi_out={out_dir}", 
               f"--plugin=protoc-gen-pyi={wrapper_path}", f"--proto_path={proto_path.parent}", str(proto_path)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
        except subprocess.CalledProcessError as e:
            sys.stderr.write(f"Error: protoc failed: {e.stderr}\n")
            sys.exit(1)
        finally:
            if wrapper_path.exists(): wrapper_path.unlink()


def read_version() -> str:
    if not VERSION_PATH.exists(): return "0.0.0"
    return VERSION_PATH.read_text(encoding="utf-8").strip()


def update_metadata(version: str):
    for f, p in [(REPO_ROOT / "pyproject.toml", r'version\s*=\s*"[^"]+"'),
                 (REPO_ROOT / "mcubridge" / "Makefile", r"PKG_VERSION:=[^\n]+"),
                 (REPO_ROOT / "mcubridge-library-arduino" / "library.properties", r"version=[^\n]+")]:
        if f.exists():
            content = f.read_text(encoding="utf-8")
            replacement = f"version = \"{version}\"" if "toml" in f.name else f"PKG_VERSION:={version}" if "Makefile" in f.name else f"version={version}"
            f.write_text(re.sub(p, replacement, content, count=1 if "toml" in f.name else 0), encoding="utf-8")


def ensure_nanopb_core_files() -> None:
    import urllib.request
    src_dir = REPO_ROOT / "mcubridge-library-arduino" / "src"
    base_url = "https://raw.githubusercontent.com/nanopb/nanopb/nanopb-0.4.9.1/"
    for f in ["pb.h", "pb_common.h", "pb_common.c", "pb_decode.h", "pb_decode.c", "pb_encode.h", "pb_encode.c"]:
        target = src_dir / f
        if not target.exists():
            with urllib.request.urlopen(base_url + f, timeout=20) as r: target.write_bytes(response.read())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--cpp", type=Path)
    parser.add_argument("--cpp-structs", type=Path)
    parser.add_argument("--py", type=Path)
    parser.add_argument("--py-client", type=Path)
    parser.add_argument("--structures", type=Path)
    args = parser.parse_args()

    spec = ProtocolSpec.load(args.spec)
    gen = JinjaGenerator()
    version = read_version()
    update_metadata(version)

    proto_path = (args.spec.parent / "mcubridge.proto").resolve()
    if proto_path.exists():
        sys.stderr.write(f"Compiling {proto_path}...\n")
        gen.generate_python_pb2(proto_path, args.spec.parent)
        gen.generate_nanopb(proto_path)

        py_pb2_temp = args.spec.parent / "mcubridge_pb2.py"
        proto_meta = gen._extract_proto_metadata(py_pb2_temp)

        if proto_meta.get("commands"):
            cmd_map = {c.name: c for c in spec.commands}
            spec.commands = [_spec_mod.CommandDef(name=pc["name"], value=pc["value"], 
                             directions=cmd_map[pc["name"]].directions if pc["name"] in cmd_map else ["both"],
                             requires_ack=cmd_map[pc["name"]].requires_ack if pc["name"] in cmd_map else False,
                             expects_direct_response=cmd_map[pc["name"]].expects_direct_response if pc["name"] in cmd_map else False)
                             for pc in proto_meta["commands"]]

        if proto_meta.get("statuses"):
            stat_map = {s.name: s for s in spec.statuses}
            spec.statuses = [_spec_mod.StatusDef(name=ps["name"], value=ps["value"], 
                             description=stat_map[ps["name"]].description if ps["name"] in stat_map else "")
                             for ps in proto_meta["statuses"]]

        if args.cpp:
            for ext in [".pb.h", ".pb.c"]:
                src, dst = args.spec.parent / ("mcubridge" + ext), args.cpp.parent / ("mcubridge" + ext)
                if src.exists():
                    data = src.read_text().replace("#include <pb.h>", '#include "../pb.h"') if ext == ".pb.h" else src.read_text()
                    dst.write_text(data)
                    src.unlink()

        for dst_path in [args.py, args.py_client]:
            if dst_path:
                for ext in [".py", ".pyi"]:
                    src = args.spec.parent / ("mcubridge_pb2" + ext)
                    if src.exists():
                        (dst_path.parent / ("mcubridge_pb2" + ext)).write_bytes(src.read_bytes())
                # unlink temp files at the end
    
    if args.cpp:
        gen.generate_cpp_header(spec, args.cpp, version)
        gen.generate_cpp_hw_config(spec, args.cpp.parent / "rpc_hw_config.h")
    
    if args.py: gen.generate_python(spec, args.py)
    if args.py_client: gen.generate_python_client(spec, args.py_client)

if __name__ == "__main__":
    main()
