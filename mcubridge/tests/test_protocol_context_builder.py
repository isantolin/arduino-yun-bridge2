from __future__ import annotations

import sys

import pytest
from packaging.version import Version

from mcubridge.protocol import mcubridge_pb2
from tools.protocol import generate


def test_build_protocol_context_contains_template_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "mcubridge_pb2", mcubridge_pb2)
    spec = generate.load_spec_from_proto(generate.REPO_ROOT / "tools" / "protocol" / "mcubridge.proto")
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
    assert context["commands"] == spec.commands
    assert context["payload_fields"]
    assert context["topic_auth_map"]
    assert context["runtime_config_fields"] == spec.runtime_config_fields
    assert context["request_response_pairs"]
