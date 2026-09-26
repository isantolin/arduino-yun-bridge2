"""Test suite for Protobuf SSOT integrity, dead definition audits, and strong typing. [SIL-2]"""

from pathlib import Path
from google.protobuf.descriptor import FieldDescriptor
from typer.testing import CliRunner

from mcubridge.protocol import mcubridge_pb2 as pb
from tools.audit.codebase_auditor import app, audit_proto_integrity

runner = CliRunner()


def test_audit_proto_integrity_clean() -> None:
    """SIL-2: Verify canonical mcubridge.proto contains zero dead or abandoned definitions."""
    findings = audit_proto_integrity()
    assert findings == []


def test_audit_proto_integrity_catches_all_dead_definitions(tmp_path: Path) -> None:
    """SIL-2: Verify auditor deterministically catches all prohibited dead blocks, fields, and constants."""
    mock_proto = tmp_path / "mcubridge.proto"
    content = """
    syntax = "proto3";
    package rpc.pb;

    message DataFormats {
        string uint8_format = 1;
    }

    message Handshake {
        string tag_algorithm = 1;
        string tag_description = 2;
        string hkdf_algorithm = 3;
        string nonce_format_description = 4;
        string aead_algorithm = 5;
        string aead_description = 6;
    }

    message Constants {
        uint32 default_serial_fallback_threshold = 1;
        uint32 cloud_expiry_shell = 2;
        uint32 cloud_expiry_default = 3;
    }

    extend google.protobuf.FileOptions {
        DataFormats data_formats = 1004;
    }

    option (rpc.pb.data_formats) = {};
    option (rpc.pb.handshake) = {
        tag_algorithm: "HMAC"
        tag_description: "desc"
        hkdf_algorithm: "HKDF"
        nonce_format_description: "nonce"
        aead_algorithm: "AEAD"
        aead_description: "desc"
    };
    option (rpc.pb.constants) = {
        default_serial_fallback_threshold: 5
        cloud_expiry_shell: 30
        cloud_expiry_default: 10
    };
    """
    mock_proto.write_text(content, encoding="utf-8")

    findings = audit_proto_integrity(mock_proto)

    assert any("data_formats" in f for f in findings)
    assert any("DataFormats" in f for f in findings)
    assert any("tag_algorithm" in f for f in findings)
    assert any("tag_description" in f for f in findings)
    assert any("hkdf_algorithm" in f for f in findings)
    assert any("nonce_format_description" in f for f in findings)
    assert any("aead_algorithm" in f for f in findings)
    assert any("aead_description" in f for f in findings)
    assert any("default_serial_fallback_threshold" in f for f in findings)
    assert any("cloud_expiry_shell" in f for f in findings)
    assert any("cloud_expiry_default" in f for f in findings)
    assert len(findings) == 11


def test_audit_proto_integrity_missing_file(tmp_path: Path) -> None:
    """SIL-2: Verify auditor returns appropriate finding when proto file does not exist."""
    missing = tmp_path / "missing.proto"
    findings = audit_proto_integrity(missing)
    assert len(findings) == 1
    assert "Protobuf File Missing" in findings[0]
    assert str(missing) in findings[0]


def test_protobuf_descriptor_purged_elements() -> None:
    """SIL-2: Verify compiled descriptor does not contain any abandoned messages or fields."""
    file_desc = pb.DESCRIPTOR

    # 1. No DataFormats message
    assert "DataFormats" not in file_desc.message_types_by_name

    # 2. No data_formats file extension
    assert "data_formats" not in file_desc.extensions_by_name

    # 3. No decorative handshake fields
    handshake_fields = pb.Handshake.DESCRIPTOR.fields_by_name
    assert "tag_algorithm" not in handshake_fields
    assert "tag_description" not in handshake_fields
    assert "hkdf_algorithm" not in handshake_fields
    assert "nonce_format_description" not in handshake_fields
    assert "aead_algorithm" not in handshake_fields
    assert "aead_description" not in handshake_fields

    # 4. No dead constants
    constant_fields = pb.Constants.DESCRIPTOR.fields_by_name
    assert "default_serial_fallback_threshold" not in constant_fields
    assert "cloud_expiry_shell" not in constant_fields
    assert "cloud_expiry_default" not in constant_fields


def test_rpc_envelope_strong_typing() -> None:
    """SIL-2: Verify RpcEnvelope channel_id and qos fields are bound to strongly-typed enums."""
    env_fields = pb.RpcEnvelope.DESCRIPTOR.fields_by_name

    # channel_id must be TYPE_ENUM bound to ChannelId
    channel_field = env_fields["channel_id"]
    assert channel_field.type == FieldDescriptor.TYPE_ENUM
    assert channel_field.enum_type is not None
    assert channel_field.enum_type.name == "ChannelId"

    # qos must be TYPE_ENUM bound to QosProfile
    qos_field = env_fields["qos"]
    assert qos_field.type == FieldDescriptor.TYPE_ENUM
    assert qos_field.enum_type is not None
    assert qos_field.enum_type.name == "QosProfile"

    # Roundtrip test
    envelope = pb.RpcEnvelope(
        version=2,
        command_id=0x01,
        sequence_id=0x0A,
        channel_id=pb.ChannelId.CHANNEL_DATA,
        qos=pb.QosProfile.QOS_BEST_EFFORT,
    )
    serialized = envelope.SerializeToString()

    restored = pb.RpcEnvelope()
    restored.ParseFromString(serialized)

    assert restored.version == 2
    assert restored.command_id == 0x01
    assert restored.sequence_id == 0x0A
    assert restored.channel_id == pb.ChannelId.CHANNEL_DATA
    assert restored.qos == pb.QosProfile.QOS_BEST_EFFORT


def test_mcubridge_options_cleanliness() -> None:
    """SIL-2: Verify mcubridge.options has no orphaned DataFormats definitions."""
    options_path = Path(__file__).resolve().parent.parent.parent / "tools" / "protocol" / "mcubridge.options"
    assert options_path.exists()
    content = options_path.read_text(encoding="utf-8")

    assert "DataFormats" not in content
    assert "data_formats" not in content


def test_codebase_auditor_cli_success() -> None:
    """MIL-SPEC: Verify codebase auditor CLI command runs and passes 100% cleanly."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Auditing Protobuf definitions..." in result.stdout
    assert "No violations or shims found!" in result.stdout
