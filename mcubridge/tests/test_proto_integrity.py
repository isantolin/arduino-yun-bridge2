"""Test suite for Protobuf SSOT integrity, dead definition audits, and strong typing. [SIL-2]"""

from collections.abc import Sequence
from pathlib import Path
import subprocess

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import FieldDescriptor
import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol import protocol
from tools.audit import codebase_auditor
from tools.audit.codebase_auditor import app, audit_ast_integrity, audit_proto_integrity, audit_proto_usage

runner = CliRunner()


def test_audit_proto_integrity_clean() -> None:
    """SIL-2: Verify canonical mcubridge.proto contains zero dead or abandoned definitions."""
    findings = audit_proto_integrity()
    assert findings == []


def test_audit_proto_usage_clean() -> None:
    assert audit_proto_usage() == []


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


def test_audit_semgrep_reports_missing_executable(tmp_path: Path, mocker: MockerFixture) -> None:
    (tmp_path / ".semgrep.yml").write_text("rules: []\n", encoding="utf-8")
    mocker.patch.object(codebase_auditor, "ROOT", tmp_path)
    mocker.patch.object(codebase_auditor.shutil, "which", return_value=None)
    mocker.patch.object(codebase_auditor.os, "access", return_value=False)

    assert codebase_auditor.audit_semgrep() == ["Semgrep Executable Missing: 'semgrep' binary not found in PATH"]


def test_audit_semgrep_reports_execution_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    (tmp_path / ".semgrep.yml").write_text("rules: []\n", encoding="utf-8")
    mocker.patch.object(codebase_auditor, "ROOT", tmp_path)
    mocker.patch.object(codebase_auditor.shutil, "which", return_value="semgrep")

    def _mock_cmd_fail(cmd: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(cmd), 2, stdout='{"results":[]}', stderr="invalid rules")

    mocker.patch.object(codebase_auditor, "run_command", _mock_cmd_fail)

    assert codebase_auditor.audit_semgrep() == ["Semgrep Execution Error: invalid rules"]


def test_audit_semgrep_reports_empty_output_as_execution_error(tmp_path: Path, mocker: MockerFixture) -> None:
    """SIL-2: Verify empty Semgrep stdout is never treated as a clean audit, even on exit code 0."""
    (tmp_path / ".semgrep.yml").write_text("rules: []\n", encoding="utf-8")
    mocker.patch.object(codebase_auditor, "ROOT", tmp_path)
    mocker.patch.object(codebase_auditor.shutil, "which", return_value="semgrep")

    def _mock_cmd_empty(cmd: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(cmd), 0, stdout="   \n", stderr="")

    mocker.patch.object(codebase_auditor, "run_command", _mock_cmd_empty)

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


def test_audit_ast_integrity_detects_violations_and_skips_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codebase_auditor, "ROOT", tmp_path)
    # Valid file
    (tmp_path / "valid.py").write_text("x = 1 + 2\n", encoding="utf-8")
    # Except with pass
    (tmp_path / "bad_except.py").write_text("try:\n    a = 1\nexcept Exception:\n    pass\n", encoding="utf-8")
    # Match case with pass
    (tmp_path / "bad_case.py").write_text("match 1:\n    case _:\n        pass\n", encoding="utf-8")
    # TYPE_CHECKING identifier
    (tmp_path / "bad_tc.py").write_text("from typing import TYPE_CHECKING\n", encoding="utf-8")
    # Syntax error file
    (tmp_path / "invalid_syntax.py").write_text("def broken(\n", encoding="utf-8")
    # Ignored directory
    ignored_dir = tmp_path / ".tox"
    ignored_dir.mkdir()
    (ignored_dir / "skip.py").write_text("try:\n    pass\nexcept Exception:\n    pass\n", encoding="utf-8")

    findings = audit_ast_integrity()

    assert any("bad_except.py" in f and "Catching exceptions with 'pass'" in f for f in findings)
    assert any("bad_case.py" in f and "Match case with 'pass'" in f for f in findings)
    assert any("bad_tc.py" in f and "TYPE_CHECKING dual-standard" in f for f in findings)
    assert any("invalid_syntax.py" in f and "AST Parse Error" in f for f in findings)
    assert not any("skip.py" in f for f in findings)
    assert not any("valid.py" in f for f in findings)


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

    # 4. Retired constants are absent and their field numbers/names remain reserved.
    constant_fields = pb.Constants.DESCRIPTOR.fields_by_name
    retired_constants = {
        "max_command_id",
        "invalid_id_sentinel",
        "rpc_null_terminator",
        "rpc_command_stride",
        "rpc_command_group_shift",
        "rpc_command_group_offset",
        "rpc_timer_overflow_threshold",
        "process_default_exit_code",
        "min_frame_size",
        "crc_initial",
        "crc_polynomial",
        "digital_low",
        "digital_high",
        "gpio_command_min",
        "gpio_command_max",
        "console_command_min",
        "console_command_max",
        "datastore_command_min",
        "datastore_command_max",
        "mailbox_command_min",
        "mailbox_command_max",
        "filesystem_command_min",
        "filesystem_command_max",
        "process_command_min",
        "process_command_max",
        "spi_command_min",
        "spi_command_max",
        "default_reconnect_delay",
        "clock_command_min",
        "clock_command_max",
    }
    assert retired_constants.isdisjoint(constant_fields)

    descriptor_file = descriptor_pb2.FileDescriptorProto()
    descriptor_file.ParseFromString(pb.Constants.DESCRIPTOR.file.serialized_pb)
    constants_proto = next(message for message in descriptor_file.message_type if message.name == "Constants")
    reserved_constant_numbers = {
        number for item in constants_proto.reserved_range for number in range(item.start, item.end)
    }
    expected_reserved_numbers = set(range(14, 21)) | {24, 27, 29, 31, 33, 34} | set(range(39, 54)) | {67, 68}
    assert expected_reserved_numbers <= reserved_constant_numbers
    assert retired_constants <= set(constants_proto.reserved_name)

    channel_descriptor_file = descriptor_pb2.FileDescriptorProto()
    channel_descriptor_file.ParseFromString(pb.ChannelId.DESCRIPTOR.file.serialized_pb)
    channel_enum = next(enum for enum in channel_descriptor_file.enum_type if enum.name == "ChannelId")
    reserved_channel_numbers = {
        number for item in channel_enum.reserved_range for number in range(item.start, item.end + 1)
    }
    assert {1, 3} <= reserved_channel_numbers
    assert {"CHANNEL_CONSOLE", "CHANNEL_DATA"} <= set(channel_enum.reserved_name)
    assert {"CHANNEL_CONSOLE", "CHANNEL_DATA"}.isdisjoint(pb.ChannelId.DESCRIPTOR.values_by_name)


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
    pre_sync_specs = {
        spec.command_id
        for spec in protocol.REGISTRY.operations
        if spec.command_id is not None and spec.pre_sync_allowed
    }
    assert {protocol.Command[name].value for name in expected_names} == pre_sync_specs


def test_topic_lookup_matches_schema_metadata() -> None:
    topic_configs = pb.Constants.DESCRIPTOR.file.GetOptions().Extensions[pb.topics]
    for topic_config in topic_configs:
        enum_val = protocol.Topic[topic_config.name]
        assert protocol.REGISTRY.lookup_topic(topic_config.value) == enum_val
        for alias in topic_config.aliases:
            assert protocol.REGISTRY.lookup_topic(alias) == enum_val


def test_command_specs_multidimensional_integrity() -> None:
    """Verify that every command has a canonical OperationSpec matching Protobuf descriptors."""
    commands_count = len(pb.Command.DESCRIPTOR.values) - 1
    assert len(protocol.REGISTRY.by_command_id) == commands_count
    for val in pb.Command.DESCRIPTOR.values:
        if val.name == "CMD_UNSPECIFIED":
            continue
        spec = protocol.REGISTRY.get(val.number)
        assert spec is not None
        assert spec.command_id == val.number
        assert spec.name == val.name
        opts = val.GetOptions().Extensions[pb.cmd_opts]
        assert spec.requires_ack == opts.requires_ack
        assert spec.expects_direct_response == bool(opts.response_command)
        assert spec.pre_sync_allowed == opts.pre_sync_allowed
        assert spec.cloud_topic == (opts.cloud_topic or None)
        assert spec.auth_field == (opts.auth_field or None)
        if opts.pb_message:
            assert spec.pb_class is getattr(pb, opts.pb_message)
            assert spec.payload_field is not None
            msg_spec = protocol.REGISTRY.get(opts.pb_message)
            assert msg_spec is not None
            assert msg_spec.pb_class is spec.pb_class
        if opts.response_command:
            assert spec.expected_responses == frozenset({getattr(protocol.Command, opts.response_command).value})
        if spec.paired_request_id:
            assert any(
                s.expected_responses and spec.command_id in s.expected_responses for s in protocol.REGISTRY.operations
            )


def test_runtime_config_defaults_reference_canonical_metadata() -> None:
    fields = pb.RuntimeConfig.DESCRIPTOR.fields_by_name
    expected_references = {
        "serial_baud": "constants.default_baudrate",
        "serial_safe_baud": "constants.default_safe_baudrate",
        "cloud_port": "constants.default_cloud_port",
        "topic_prefix": "cloud_defaults.default_topic_prefix",
        "console_queue_limit_bytes": "constants.default_console_queue_limit_bytes",
        "serial_handshake_fatal_failures": "constants.default_serial_handshake_fatal_failures",
        "process_max_output_bytes": "constants.default_process_max_output_bytes",
        "cloud_http3_port": "constants.default_cloud_port",
    }
    for field_name, reference in expected_references.items():
        options = fields[field_name].GetOptions()
        assert options.HasExtension(pb.config_default_ref)
        assert options.Extensions[pb.config_default_ref] == reference
        assert not options.HasExtension(pb.config_default)

    assert protocol.RUNTIME_CONFIG_DEFAULTS["serial_baud"] == protocol.DEFAULT_BAUDRATE
    assert protocol.RUNTIME_CONFIG_DEFAULTS["serial_safe_baud"] == protocol.DEFAULT_SAFE_BAUDRATE
    assert protocol.RUNTIME_CONFIG_DEFAULTS["cloud_port"] == protocol.DEFAULT_CLOUD_PORT
    assert protocol.RUNTIME_CONFIG_DEFAULTS["cloud_http3_port"] == protocol.DEFAULT_CLOUD_PORT
    assert protocol.RUNTIME_CONFIG_DEFAULTS["topic_prefix"] == protocol.CLOUD_DEFAULT_TOPIC_PREFIX
    assert protocol.RUNTIME_CONFIG_DEFAULTS["reconnect_delay"] == protocol.DEFAULT_RECONNECT_DELAY
    assert protocol.RUNTIME_CONFIG_DEFAULTS["console_queue_limit_bytes"] == protocol.DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    assert (
        protocol.RUNTIME_CONFIG_DEFAULTS["serial_handshake_fatal_failures"]
        == protocol.DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES
    )
    assert protocol.RUNTIME_CONFIG_DEFAULTS["process_max_output_bytes"] == protocol.DEFAULT_PROCESS_MAX_OUTPUT_BYTES


def test_spi_config_uses_canonical_enums() -> None:
    fields = pb.SpiConfig.DESCRIPTOR.fields_by_name
    assert fields["bit_order"].type == FieldDescriptor.TYPE_ENUM
    assert fields["bit_order"].enum_type is not None
    assert fields["bit_order"].enum_type.name == "SpiBitOrder"
    assert fields["data_mode"].type == FieldDescriptor.TYPE_ENUM
    assert fields["data_mode"].enum_type is not None
    assert fields["data_mode"].enum_type.name == "SpiDataMode"
    assert {value.name: value.number for value in fields["data_mode"].enum_type.values} == {
        "SPI_DATA_MODE_0": 0,
        "SPI_DATA_MODE_1": 4,
        "SPI_DATA_MODE_2": 8,
        "SPI_DATA_MODE_3": 12,
    }


@pytest.mark.parametrize(
    "data_mode_name",
    [
        pb.SpiDataMode.Name(pb.SpiDataMode.SPI_DATA_MODE_0),
        pb.SpiDataMode.Name(pb.SpiDataMode.SPI_DATA_MODE_1),
        pb.SpiDataMode.Name(pb.SpiDataMode.SPI_DATA_MODE_2),
        pb.SpiDataMode.Name(pb.SpiDataMode.SPI_DATA_MODE_3),
    ],
)
def test_spi_data_modes_round_trip(data_mode_name: str) -> None:
    data_mode = pb.SpiDataMode.Value(data_mode_name)
    restored = pb.SpiConfig.FromString(pb.SpiConfig(data_mode=data_mode).SerializeToString())
    assert restored.data_mode == data_mode


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
        channel_id=pb.ChannelId.CHANNEL_CONTROL,
        qos=pb.QosProfile.QOS_BEST_EFFORT,
    )
    serialized = envelope.SerializeToString()

    restored = pb.RpcEnvelope()
    restored.ParseFromString(serialized)

    assert restored.version == 2
    assert restored.command_id == 0x01
    assert restored.sequence_id == 0x0A
    assert restored.channel_id == pb.ChannelId.CHANNEL_CONTROL
    assert restored.qos == pb.QosProfile.QOS_BEST_EFFORT


def test_mcubridge_options_cleanliness() -> None:
    """SIL-2: Verify protocol options files have no orphaned DataFormats definitions."""
    protocol_dir = Path(__file__).resolve().parent.parent.parent / "tools" / "protocol"
    options_files = list(protocol_dir.glob("*.options"))
    assert options_files
    for opt_path in options_files:
        content = opt_path.read_text(encoding="utf-8")
        assert "DataFormats" not in content
        assert "data_formats" not in content


def _mock_empty_findings() -> list[str]:
    return []


@pytest.mark.timeout(180)
@pytest.mark.xdist_group("repo_audit")
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


def test_proto_dispatch_handlers_ssot() -> None:
    """SIL-2: Verify dispatch tables and action routes are derived from .proto SSOT."""
    import operator
    from mcubridge.protocol import mcubridge_pb2 as pb
    from mcubridge.services.runtime import BridgeService

    assert len(protocol.REGISTRY.mcu_dispatch_handlers) == 26
    for code, handler_path in protocol.REGISTRY.mcu_dispatch_handlers.items():
        assert isinstance(code, int)
        root_attr = handler_path.split(".", 1)[0]
        assert hasattr(BridgeService, root_attr) or root_attr == "handshake"
        assert callable(operator.attrgetter(root_attr))

    assert len(protocol.REGISTRY.mcu_unsupported_reasons) == 2
    assert protocol.REGISTRY.mcu_unsupported_reasons[protocol.Command.CMD_DIGITAL_READ.value] == (
        protocol.STATUS_REASON_UNSUPPORTED_DIGITAL_READ
    )
    assert protocol.REGISTRY.mcu_unsupported_reasons[protocol.Command.CMD_ANALOG_READ.value] == (
        protocol.STATUS_REASON_UNSUPPORTED_ANALOG_READ
    )

    assert len(protocol.REGISTRY.rpc_specs) == 25
    from mcubridge.services.local_bridge import LocalBridgeService

    service_desc = next(
        dep.services_by_name["LocalBridge"]
        for dep in [pb.DESCRIPTOR, *pb.DESCRIPTOR.dependencies]
        if "LocalBridge" in dep.services_by_name
    )
    unary_methods = {m.name: m for m in service_desc.methods if not m.client_streaming and not m.server_streaming}
    assert len(unary_methods) == 25

    for spec in protocol.REGISTRY.rpc_specs:
        method_name = spec.rpc_name or spec.name
        assert method_name in unary_methods
        method_desc = unary_methods[method_name]
        assert spec.request_type is getattr(pb, method_desc.input_type.name)
        assert spec.response_type is getattr(pb, method_desc.output_type.name)
        assert spec.handler_name is not None
        assert hasattr(LocalBridgeService, spec.handler_name)
        assert callable(getattr(LocalBridgeService, spec.handler_name))
        assert hasattr(LocalBridgeService, method_name)
        assert callable(getattr(LocalBridgeService, method_name))


def test_hardware_options_completeness_ssot() -> None:
    """SIL-2: Verify all Hardware fields in config_schema.proto are > 0 and C++ headers delegate to SSoT."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    hw_opts = pb.Hardware.DESCRIPTOR.file.GetOptions().Extensions[pb.hardware]
    zero_fields = [f.name for f in pb.Hardware.DESCRIPTOR.fields if getattr(hw_opts, f.name) <= 0]
    assert zero_fields == [], f"Hardware option fields with zero/unset values: {zero_fields}"

    rpc_hw_header = (repo_root / "mcubridge-library-arduino/src/protocol/rpc_hw_config.h").read_text(encoding="utf-8")
    assert "= 0U;" not in rpc_hw_header
    assert "= 0UL;" not in rpc_hw_header

    bridge_cfg = (repo_root / "mcubridge-library-arduino/src/config/bridge_config.h").read_text(encoding="utf-8")
    assert "bridge::config::MAX_MAILBOX_QUEUE_SIZE" in bridge_cfg

    mailbox_hdr = (repo_root / "mcubridge-library-arduino/src/services/Mailbox.h").read_text(encoding="utf-8")
    assert "bridge::config::MAX_MAILBOX_QUEUE_SIZE" in mailbox_hdr
    assert "rpc::MAX_PAYLOAD_SIZE" in mailbox_hdr


def test_default_configuration_centralization_ssot() -> None:
    """SIL-2: Verify OpenWrt scripts, LuCI Web UI, and Cloud Gateway consume defaults from SSoT artifacts."""
    from tools.audit.validate_luci import validate_luci_app

    repo_root = Path(__file__).resolve().parent.parent.parent

    shell_consumers = [
        repo_root / "3_install.sh",
        repo_root / "mcubridge/mcubridge.init",
        repo_root / "mcubridge/uci-defaults/96-mcubridge-flash-protection-paths",
        repo_root / "mcubridge/uci-defaults/97-mcubridge-default-secret",
        repo_root / "mcubridge/uci-defaults/98-mcubridge-default-serial",
        repo_root / "mcubridge/uci-defaults/99-mcubridge-disable-askconsole",
    ]
    for script_path in shell_consumers:
        content = script_path.read_text(encoding="utf-8")
        assert "defaults.sh" in content, f"{script_path.name} must source defaults.sh"
        assert "MCUBRIDGE_DEFAULT_" in content, f"{script_path.name} must use MCUBRIDGE_DEFAULT_* variables"

    install_content = (repo_root / "3_install.sh").read_text(encoding="utf-8")
    assert "\nDEFAULT_SERIAL_RESPONSE_TIMEOUT=" not in install_content
    assert "0.75" not in install_content

    gateway_content = (repo_root / "mcubridge-gateway/gateway.py").read_text(encoding="utf-8")
    assert "DEFAULT_CLOUD_HOST" in gateway_content
    assert "DEFAULT_CLOUD_PORT" in gateway_content
    assert "8843" not in gateway_content

    assert validate_luci_app(repo_root) == 0

