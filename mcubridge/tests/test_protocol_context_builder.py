from __future__ import annotations

from packaging.version import Version

from mcubridge.protocol import mcubridge_pb2
from tools.protocol import generate


def test_build_protocol_context_contains_template_data() -> None:
    spec = generate.load_spec_from_proto(
        generate.REPO_ROOT / "tools" / "protocol" / "cloud_gateway.proto",
        pb_module=mcubridge_pb2,
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
    assert context["commands"] == spec["commands"]
    assert context["payload_fields"]
    assert context["topic_auth_map"]
    assert context["runtime_config_fields"] == spec["runtime_config_fields"]
    assert context["operations"]
    metrics = {metric["source_field"]: metric for metric in context["metrics"]}
    assert metrics["cloud_dropped_messages"]["kind"] == "MetricKind.COUNTER"
    assert metrics["retries"]["value_field"] == "count"
    assert metrics["retries"]["labels"] == [{"name": "component", "field": "component", "static_value": None}]
    assert metrics["serial_latency_ms"]["metric_name"] == metrics["rpc_latency_ms"]["metric_name"]
    assert [field["field_name"] for field in context["line_protocol_fields"]] == [
        "queue_depth",
        "dropped_messages",
        "spool_pending",
        "spool_degraded",
        "link_synchronized",
        "watchdog_enabled",
        "serial_bytes_sent",
        "serial_bytes_received",
        "serial_frames_sent",
        "serial_frames_received",
        "serial_crc_errors",
        "serial_decode_errors",
        "watchdog_beats",
        "published_messages",
    ]
    assert {constant["name"]: constant["value"] for constant in context["handshake_constants"]}[
        "RPC_MCU_AEAD_NONCE_PREFIX"
    ] == 1296258304
    assert {constant["name"]: constant["value"] for constant in context["handshake_constants"]}[
        "RPC_MPU_AEAD_NONCE_PREFIX"
    ] == 1297110272
    assert {constant["name"]: constant["value"] for constant in context["python_handshake_constants"]}[
        "HANDSHAKE_HKDF_INFO_SESSION"
    ] == 'b"session-key"'
