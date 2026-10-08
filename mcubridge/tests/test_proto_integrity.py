"""Test suite for Protobuf SSOT integrity, dead definition audits, and strong typing. [SIL-2]"""

from collections.abc import Sequence
from pathlib import Path
import subprocess

from google.protobuf.descriptor import FieldDescriptor
import pytest
from typer.testing import CliRunner

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from tools.audit import codebase_auditor
from tools.audit.codebase_auditor import app, audit_proto_integrity

runner = CliRunner()


def _mock_which_none(_name: str) -> None:
    return None


def _mock_which_semgrep(_name: str) -> str:
    return "semgrep"


def test_audit_proto_integrity_clean() -> None:
    """SIL-2: Verify canonical mcubridge.proto contains zero dead or abandoned definitions."""
    findings = audit_proto_integrity()
    assert findings == []


def test_audit_proto_integrity_catches_buf_violations(tmp_path: Path) -> None:
    """SIL-2: Verify auditor deterministically catches Protobuf violations using Buf."""
    mock_proto = tmp_path / "invalid.proto"
    content = """syntax = "proto3";

package rpc.pb;

message InvalidCasing {
  string BadCamelCaseField = 1;
}
"""
    mock_proto.write_text(content, encoding="utf-8")

    findings = audit_proto_integrity(mock_proto)

    assert len(findings) >= 1
    assert "Buf Lint Violation" in findings[0]
    assert "lower_snake_case" in findings[0]


def test_audit_proto_integrity_missing_file(tmp_path: Path) -> None:
    """SIL-2: Verify auditor returns appropriate finding when proto file does not exist."""
    missing = tmp_path / "missing.proto"
    findings = audit_proto_integrity(missing)
    assert len(findings) == 1
    assert "Protobuf File Missing" in findings[0] or "Protobuf spec missing" in findings[0]
    assert str(missing) in findings[0]


def test_audit_semgrep_reports_missing_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".semgrep.yml").write_text("rules: []\n", encoding="utf-8")
    monkeypatch.setattr(codebase_auditor, "ROOT", tmp_path)
    monkeypatch.setattr(codebase_auditor.shutil, "which", _mock_which_none)

    assert codebase_auditor.audit_semgrep() == ["Semgrep Executable Missing: 'semgrep' binary not found in PATH"]


def test_audit_semgrep_reports_execution_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".semgrep.yml").write_text("rules: []\n", encoding="utf-8")
    monkeypatch.setattr(codebase_auditor, "ROOT", tmp_path)
    monkeypatch.setattr(codebase_auditor.shutil, "which", _mock_which_semgrep)

    def _mock_cmd_fail(cmd: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(cmd), 2, stdout='{"results":[]}', stderr="invalid rules")

    monkeypatch.setattr(codebase_auditor, "run_command", _mock_cmd_fail)

    assert codebase_auditor.audit_semgrep() == ["Semgrep Execution Error: invalid rules"]


def test_audit_semgrep_reports_empty_output_as_execution_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SIL-2: Verify empty Semgrep stdout is never treated as a clean audit, even on exit code 0."""
    (tmp_path / ".semgrep.yml").write_text("rules: []\n", encoding="utf-8")
    monkeypatch.setattr(codebase_auditor, "ROOT", tmp_path)
    monkeypatch.setattr(codebase_auditor.shutil, "which", _mock_which_semgrep)

    def _mock_cmd_empty(cmd: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(cmd), 0, stdout="   \n", stderr="")

    monkeypatch.setattr(codebase_auditor, "run_command", _mock_cmd_empty)

    assert codebase_auditor.audit_semgrep() == ["Semgrep Execution Error: Empty output received from Semgrep"]


def test_audit_config_suppressions_reports_matches_and_skips_ignored_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codebase_auditor, "ROOT", tmp_path)
    (tmp_path / "valid.yml").write_text("key: value\n", encoding="utf-8")
    (tmp_path / "suppressed.yml").write_text("ignore_errors: true\n", encoding="utf-8")
    ignored_dir = tmp_path / ".tox"
    ignored_dir.mkdir()
    (ignored_dir / "skip.yml").write_text("ignore_errors: true\n", encoding="utf-8")

    findings = codebase_auditor.audit_config_suppressions()

    assert len(findings) == 1
    assert "suppressed.yml" in findings[0]
    assert "skip.yml" not in findings[0]


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


def test_telemetry_routing_table_matches_schema_metadata() -> None:
    expected_map = {
        "metrics": "daemon_metrics_blob",
        "summary": "bridge_snapshot_blob",
        "handshake": "handshake_snapshot_blob",
    }
    assert expected_map == protocol.TELEMETRY_TOPIC_FIELD_MAP
    assert protocol.TELEMETRY_DEFAULT_FIELD == "system_status_blob"

    fields_by_name = pb.TelemetryReport.DESCRIPTOR.fields_by_name
    assert set(expected_map.values()) | {protocol.TELEMETRY_DEFAULT_FIELD} == set(fields_by_name)
    for topic_match, field_name in expected_map.items():
        options = fields_by_name[field_name].GetOptions()
        assert options.HasExtension(pb.telemetry_topic_match)
        assert options.Extensions[pb.telemetry_topic_match] == topic_match

    default_options = fields_by_name[protocol.TELEMETRY_DEFAULT_FIELD].GetOptions()
    assert default_options.HasExtension(pb.telemetry_topic_default)
    assert default_options.Extensions[pb.telemetry_topic_default] is True


def test_pre_sync_command_set_matches_schema_metadata() -> None:
    expected_names = {"CMD_LINK_SYNC_RESP", "CMD_LINK_RESET_RESP"}
    command_values = pb.Command.DESCRIPTOR.values
    actual_names = {
        command.name for command in command_values if command.GetOptions().Extensions[pb.cmd_opts].pre_sync_allowed
    }

    assert expected_names == actual_names
    assert {protocol.Command[name].value for name in expected_names} == protocol.PRE_SYNC_ALLOWED_COMMANDS


def test_topic_aliases_match_schema_metadata() -> None:
    topic_configs = pb.DESCRIPTOR.GetOptions().Extensions[pb.topics]
    expected_aliases = {
        alias: protocol.Topic[topic_config.name] for topic_config in topic_configs for alias in topic_config.aliases
    }

    assert expected_aliases == protocol.TOPIC_ALIASES


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


def _mock_empty_findings() -> list[str]:
    return []


def test_codebase_auditor_cli_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """MIL-SPEC: Verify codebase auditor CLI command runs and passes 100% cleanly."""
    if not codebase_auditor.shutil.which("semgrep"):
        monkeypatch.setattr(codebase_auditor, "audit_semgrep", _mock_empty_findings)
    if not codebase_auditor.shutil.which("buf"):
        monkeypatch.setattr(codebase_auditor, "audit_proto_integrity", _mock_empty_findings)

    result = runner.invoke(app, [])
    error_msg = f"Codebase auditor failed: stdout={result.stdout}, exception={result.exception}"
    assert result.exit_code == 0, error_msg
    assert "Auditing Protobuf definitions..." in result.stdout
    assert "No violations or shims found!" in result.stdout


def test_constants_completeness() -> None:
    assert protocol.PROTOCOL_VERSION == 2
