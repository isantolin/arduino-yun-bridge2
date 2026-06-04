#!/usr/bin/env python3
"""
Evolved Master Protocol Generator (Refactored & Simplified). [SIL-2]
Centralized Logic in .proto -> Reflective Generation -> Direct Library Usage.
"""

from __future__ import annotations
import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

# --- Dependency Check ---
def _check_deps():
    deps = ["msgspec", "jinja2", "google.protobuf", "nanopb"]
    missing = [d for d in deps if importlib.util.find_spec(d.split(".")[0]) is None]
    if missing:
        sys.exit(f"ERROR: Missing dependencies: {missing}")

_check_deps()

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = Path(__file__).parent / "templates"
VERSION_PATH = REPO_ROOT / "VERSION"

# --- Import ProtocolSpec ---
_SPEC_MODEL_PATH = REPO_ROOT / "mcubridge" / "mcubridge" / "protocol" / "spec_model.py"
_loader = importlib.util.spec_from_file_location("spec_model", str(_SPEC_MODEL_PATH))
_spec_mod = importlib.util.module_from_spec(_loader)
_loader.loader.exec_module(_spec_mod)
ProtocolSpec = _spec_mod.ProtocolSpec

# --- Helper Functions ---
def camel_case(s: str) -> str:
    parts = s.split('_')
    if parts[0] in ('CMD', 'STATUS'): parts = parts[1:]
    if parts[-1] == 'RESP': parts[-1] = 'Response'
    return "".join(p.capitalize() for p in parts)

def get_payload_type(cmd_name: str, messages: list[str]) -> str | None:
    cc = camel_case(cmd_name)
    if cc in messages: return cc
    if cc.endswith("Response"):
        if cc[:-8] in messages: return cc[:-8]
        if cc.startswith("Get") and cc[3:] in messages: return cc[3:]
        if cc.startswith("Get") and cc[3:-8] in messages: return cc[3:-8]
    overrides = {"CMD_SET_PIN_MODE": "PinMode", "CMD_DIGITAL_WRITE": "DigitalWrite", "CMD_ANALOG_WRITE": "AnalogWrite",
                 "CMD_DIGITAL_READ": "PinRead", "CMD_ANALOG_READ": "PinRead", "CMD_SPI_SET_CONFIG": "SpiConfig"}
    return overrides.get(cmd_name)

def cpp_digits(value: Any) -> str:
    if isinstance(value, int) and value >= 10000:
        if value == 65535: return str(value)
        return f"{value:_}".replace("_", "'")
    return str(value)

def get_cpp_type(name: str, val: Any) -> str:
    if isinstance(val, bool): return "bool"
    if 'NONCE_COUNTER_MASK' in name: return 'uint64_t'
    if "BAUDRATE" in name: return "unsigned long"
    if "TIMEOUT" in name or "MAGIC" in name or "THRESHOLD" in name or "MASK" in name or "POLYNOMIAL" in name: return "uint32_t"
    if "SIZE" in name or "LENGTH" in name or "LIMIT" in name: return "size_t"
    if "VERSION" in name or "ID" in name: return "uint8_t" if "VERSION" in name else "uint16_t"
    if isinstance(val, int): return "uint32_t" if val > 0xFFFF else "uint16_t" if val > 0xFF else "uint8_t"
    return "int"

def get_py_type(val: Any) -> str:
    if isinstance(val, bool): return "bool"
    if isinstance(val, int): return "int"
    if isinstance(val, float): return "float"
    return "str"

# --- Protocol Reflection ---
class ProtoReflector:
    def __init__(self, proto_dir: Path):
        self.proto_dir = proto_dir
        self.protoc = REPO_ROOT / "bin" / "protoc" if (REPO_ROOT / "bin" / "protoc").exists() else Path("protoc")

    def reflect(self) -> tuple[list[dict], list[dict], list[str]]:
        import nanopb
        nanopb_proto_dir = Path(nanopb.__file__).parent / "generator" / "proto"
        with tempfile.TemporaryDirectory() as tmp:
            cmd_nanopb = [str(self.protoc), f"--python_out={tmp}", f"--proto_path={nanopb_proto_dir}", str(nanopb_proto_dir / "nanopb.proto")]
            subprocess.run(cmd_nanopb, check=True, capture_output=True)
            cmd = [str(self.protoc), f"--python_out={tmp}", f"--proto_path={self.proto_dir}", f"--proto_path={nanopb_proto_dir}", str(self.proto_dir / "bridge_options.proto"), str(self.proto_dir / "mcubridge.proto")]
            subprocess.run(cmd, check=True, capture_output=True)
            sys.path.insert(0, tmp)
            sys.path.insert(0, str(REPO_ROOT / "mcubridge"))
            if "mcubridge_pb2" in sys.modules: del sys.modules["mcubridge_pb2"]
            if "bridge_options_pb2" in sys.modules: del sys.modules["bridge_options_pb2"]
            import bridge_options_pb2, mcubridge_pb2
            msgs = [m.name for m in mcubridge_pb2.DESCRIPTOR.message_types_by_name.values()]
            cmds = []
            for v in mcubridge_pb2.CommandId.DESCRIPTOR.values:
                if v.number == 0: continue
                o = v.GetOptions()
                try: category = o.Extensions[bridge_options_pb2.category]
                except KeyError: category = None
                try: requires_ack = o.Extensions[bridge_options_pb2.requires_ack]
                except KeyError: requires_ack = False
                try: description = o.Extensions[bridge_options_pb2.description]
                except KeyError: description = None
                try: response_message = o.Extensions[bridge_options_pb2.response_message]
                except KeyError: response_message = None
                
                payload = get_payload_type(v.name, msgs)
                cmds.append({"name": v.name, "value": v.number, "category": category, "requires_ack": requires_ack, "description": description, "expects_direct_response": bool(response_message), "payload_type": payload})
            stats = [{"name": v.name.replace("STATUS_", ""), "value": v.number, "description": ""} for v in mcubridge_pb2.StatusCode.DESCRIPTOR.values if v.number != 0]
            return cmds, stats, msgs

# --- Generator ---
class Generator:
    def __init__(self, spec: ProtocolSpec, version: str):
        self.spec = spec
        self.version = version
        self.env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
        self.env.globals.update({'camel_case': camel_case, 'get_payload_type': get_payload_type})
        self.env.filters['tojson'] = lambda v: 'True' if v is True else 'False' if v is False else json.dumps(v)
        self.env.filters['cpp_digits'] = cpp_digits

    def render(self, template_name: str, out_path: Path):
        template = self.env.get_template(template_name)
        v_major, v_minor, v_patch = map(int, self.version.split("."))
        is_py = out_path.suffix == ".py"
        all_consts = dict(self.spec.constants)
        all_consts.update(self.spec.hardware)
        constants = []
        for k, v in all_consts.items():
            name = k.upper()
            constants.append({"name": name, "type": get_py_type(v) if is_py else get_cpp_type(name, v), "value": v})
        
        constants.append({"name": "FIRMWARE_VERSION_MAJOR", "type": "uint8_t", "value": v_major})
        constants.append({"name": "FIRMWARE_VERSION_MINOR", "type": "uint8_t", "value": v_minor})
        constants.append({"name": "FIRMWARE_VERSION_PATCH", "type": "uint8_t", "value": v_patch})

        handshake_constants = []
        for k, v in self.spec.handshake.items():
            if isinstance(v, (int, float, bool)):
                name = f"HANDSHAKE_{k.upper()}"
                handshake_constants.append({"name": name, "type": get_py_type(v) if is_py else get_cpp_type(name, v), "value": v})
        ctx = {"spec": self.spec, "version": self.version, "v_major": v_major, "v_minor": v_minor, "v_patch": v_patch, "commands": self.spec.commands, "statuses": self.spec.statuses, "hardware": self.spec.hardware, "constants": constants, "handshake_constants": handshake_constants, "handshake": { "hkdf_salt": self.spec.handshake.get("hkdf_salt"), "hkdf_salt_len": len(self.spec.handshake.get("hkdf_salt", "")), "hkdf_salt_bytes": ", ".join(f"0x{ord(char):02X}" for char in self.spec.handshake.get("hkdf_salt", "")), "hkdf_info_auth": self.spec.handshake.get("hkdf_info_auth"), "hkdf_info_auth_len": len(self.spec.handshake.get("hkdf_info_auth", "")), "hkdf_info_auth_bytes": ", ".join(f"0x{ord(char):02X}" for char in self.spec.handshake.get("hkdf_info_auth", "")), "hkdf_info_session": self.spec.handshake.get("hkdf_info_session"), "hkdf_info_session_len": len(self.spec.handshake.get("hkdf_info_session", "")), "hkdf_info_session_bytes": ", ".join(f"0x{ord(char):02X}" for char in self.spec.handshake.get("hkdf_info_session", "")) }, "handshake_strings": {f"HANDSHAKE_{k.upper()}": v for k, v in self.spec.handshake.items() if isinstance(v, str) and not k.endswith("_salt") and not k.startswith("hkdf_info")}, "handshake_bytes": {f"HANDSHAKE_{k.upper()}": v for k, v in self.spec.handshake.items() if isinstance(v, str) and (k.endswith("_salt") or k.startswith("hkdf_info"))}, "compression": self.spec.compression, "capabilities": self.spec.capabilities, "architectures": self.spec.architectures, "architecture_display_names": self.spec.architecture_display_names, "status_reasons": self.spec.status_reasons, "topics": self.spec.topics, "mqtt_defaults": self.spec.mqtt_defaults, "mqtt_suffixes": self.spec.mqtt_suffixes, "ack_commands": [c for c in self.spec.commands if c.requires_ack], "response_only_commands": [c for c in self.spec.commands if c.expects_direct_response], "structs": self.spec.messages, "messages": [m.name for m in self.spec.messages], "request_response_pairs": self._build_req_resp(self.spec.commands), "grouped_actions": self._group_actions(self.spec.actions), "subscriptions": self._build_subs(self.spec.mqtt_subscriptions)}
        ctx["response_to_req_map"] = {v: k for k, vals in ctx["request_response_pairs"].items() for v in vals}
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(template.render(**ctx), encoding="utf-8")
        if is_py: subprocess.run([sys.executable, "-m", "black", "--quiet", str(out_path)], check=False)

    def _build_req_resp(self, cmds):
        res = {}
        for c in cmds:
            resp_name = f"{c.name}_RESP"
            if any(x.name == resp_name for x in cmds): res[c.name] = [resp_name]
        return res

    def _group_actions(self, actions):
        groups = {}
        for a in actions:
            prefix, suffix = a["name"].split("_", 1)
            group_key = prefix
            cls_name = "DatastoreAction" if group_key == "DATASTORE" else f"{group_key.lower().title()}Action"
            group = groups.setdefault(cls_name, [])
            if not any(x["name"] == suffix for x in group): group.append({"name": suffix, "value": a["value"], "description": a["description"]})
        return [{"class_name": k, "action_items": v} for k, v in groups.items()]

    def _build_subs(self, subs):
        for s in subs: s["segments_tuple"] = f"({', '.join(json.dumps(x) for x in s['segments'])},)"
        return subs

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--cpp", type=Path)
    parser.add_argument("--cpp-structs", type=Path); parser.add_argument("--cpp-dispatch", type=Path)
    parser.add_argument("--py", type=Path)
    parser.add_argument("--py-client", type=Path)
    args = parser.parse_args()
    version = VERSION_PATH.read_text().strip()
    spec = ProtocolSpec.load(args.spec)
    try:
        cmds, stats, msgs = ProtoReflector(args.spec.parent).reflect()
        spec.commands = [_spec_mod.CommandDef(**c, directions=[]) for c in cmds]
        spec.statuses = [_spec_mod.StatusDef(**s) for s in stats]
        spec.messages = [_spec_mod.MessageDef(name=m, fields=[]) for m in msgs]
        print(f"Evolved Master: Overrode {len(cmds)} commands from .proto")
    except Exception as e: print(f"Warning: Proto reflection failed: {e}. Using TOML.")
    gen = Generator(spec, version)
    if args.cpp:
        gen.render("rpc_protocol.h.j2", args.cpp)
        gen.render("rpc_hw_config.h.j2", args.cpp.parent / "rpc_hw_config.h")
        gen.render("rpc_dispatch.h.j2", args.cpp.parent / "rpc_dispatch.h")
    if args.cpp_structs: gen.render("rpc_structs.h.j2", args.cpp_structs)
    if args.py: gen.render("protocol.py.j2", args.py)
    if args.py_client: gen.render("protocol.py.j2", args.py_client)

if __name__ == "__main__": main()
