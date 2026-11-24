from __future__ import annotations

from packaging.version import Version

from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
from tools.protocol import generate


def test_build_protocol_context_contains_template_data() -> None:
    class _UnifiedProto:
        def __init__(self) -> None:
            for m in (config_schema_pb2, mcu_wire_pb2, cloud_gateway_pb2):
                for k in dir(m):
                    if not k.startswith("__"):
                        setattr(self, k, getattr(m, k))
            self.DESCRIPTOR = cloud_gateway_pb2.DESCRIPTOR

    spec = generate.load_spec_from_proto(
        generate.REPO_ROOT / "tools" / "protocol" / "cloud_gateway.proto",
        pb_module=_UnifiedProto(),
    )
    version = Version(generate.VERSION_PATH.read_text(encoding="utf-8").strip())

    context = generate.build_protocol_context(spec, str(version))

    assert (context["v_major"], context["v_minor"], context["v_patch"]) == (
        version.major,
        version.minor,
        version.micro,
    )
    assert {constant["name"] for constant in context["constants"]} >= {
        "FIRMWARE_VERSION_MAJOR",
        "FIRMWARE_VERSION_MINOR",
        "FIRMWARE_VERSION_PATCH",
    }
    assert context["python_constants"]
    assert context["client_constants"]
    assert context["runtime_config_fields"] == spec["runtime_config_fields"]
    assert context["operations"]
    assert {constant["name"]: constant["value"] for constant in context["python_handshake_constants"]}[
        "HANDSHAKE_HKDF_INFO_SESSION"
    ] == 'b"session-key-v2"'
