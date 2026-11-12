"""Test suite for Protobuf SSOT integrity, dead definition audits, and strong typing. [SIL-2]"""

from collections.abc import Sequence
from pathlib import Path
import subprocess
from typing import Any, cast

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import FieldDescriptor
import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
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

    def _mock_cmd_fail(
        cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        _ = env
        return subprocess.CompletedProcess(list(cmd), 2, stdout='{"results":[]}', stderr="invalid rules")

    mocker.patch.object(codebase_auditor, "run_command", _mock_cmd_fail)

    assert codebase_auditor.audit_semgrep() == ["Semgrep Execution Error: invalid rules"]


def test_audit_semgrep_reports_empty_output_as_execution_error(tmp_path: Path, mocker: MockerFixture) -> None:
    """SIL-2: Verify empty Semgrep stdout is never treated as a clean audit, even on exit code 0."""
    (tmp_path / ".semgrep.yml").write_text("rules: []\n", encoding="utf-8")
    mocker.patch.object(codebase_auditor, "ROOT", tmp_path)
    mocker.patch.object(codebase_auditor.shutil, "which", return_value="semgrep")

    def _mock_cmd_empty(
        cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        _ = env
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
    all_descriptors = (cloud_gateway_pb2.DESCRIPTOR, mcu_wire_pb2.DESCRIPTOR, config_schema_pb2.DESCRIPTOR)

    # 1. No DataFormats message
    for fd in all_descriptors:
        assert "DataFormats" not in fd.message_types_by_name

    # 2. No data_formats file extension
    for fd in all_descriptors:
        assert "data_formats" not in fd.extensions_by_name

    # 3. No decorative handshake fields
    handshake_fields = config_schema_pb2.Handshake.DESCRIPTOR.fields_by_name
    assert "tag_algorithm" not in handshake_fields
    assert "tag_description" not in handshake_fields
    assert "hkdf_algorithm" not in handshake_fields
    assert "nonce_format_description" not in handshake_fields
    assert "aead_algorithm" not in handshake_fields
    assert "aead_description" not in handshake_fields

    # 4. Retired constants are absent and their field numbers/names remain reserved.
    constant_fields = config_schema_pb2.Constants.DESCRIPTOR.fields_by_name
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
    descriptor_file.ParseFromString(config_schema_pb2.Constants.DESCRIPTOR.file.serialized_pb)
    constants_proto = next(message for message in descriptor_file.message_type if message.name == "Constants")
    reserved_constant_numbers = {
        number for item in constants_proto.reserved_range for number in range(item.start, item.end)
    }
    expected_reserved_numbers = set(range(14, 21)) | {24, 27, 29, 31, 33, 34} | set(range(39, 54)) | {67, 68}
    assert expected_reserved_numbers <= reserved_constant_numbers
    assert retired_constants <= set(constants_proto.reserved_name)


def test_telemetry_routing_table_matches_schema_metadata() -> None:
    expected_map = {
        "Metrics": "daemon_metrics_blob",
        "BridgeSnapshot": "bridge_snapshot_blob",
        "HandshakeSnapshot": "handshake_snapshot_blob",
    }
    assert expected_map == protocol.TELEMETRY_TOPIC_FIELD_MAP
    assert protocol.TELEMETRY_DEFAULT_FIELD == "system_status_blob"

    fields_by_name = cloud_gateway_pb2.TelemetryReport.DESCRIPTOR.fields_by_name
    assert set(expected_map.values()) | {protocol.TELEMETRY_DEFAULT_FIELD} == set(fields_by_name)
    for topic_match, field_name in expected_map.items():
        options = fields_by_name[field_name].GetOptions()
        assert options.HasExtension(config_schema_pb2.telemetry_topic_match)
        assert options.Extensions[config_schema_pb2.telemetry_topic_match] == topic_match

    default_options = fields_by_name[protocol.TELEMETRY_DEFAULT_FIELD].GetOptions()
    assert default_options.HasExtension(config_schema_pb2.telemetry_topic_default)
    assert default_options.Extensions[config_schema_pb2.telemetry_topic_default] is True


def test_pre_sync_command_set_matches_schema_metadata() -> None:
    expected_names = {"rpc.pb.SystemService/LinkSync"}
    pre_sync_specs = {spec.name for spec in protocol.OPERATIONS if spec.pre_sync_allowed}
    assert expected_names == pre_sync_specs


def test_pw_services_integrity() -> None:
    """Verify application and upstream Pigweed transfer descriptors are registered."""
    assert len(protocol.PW_SERVICES) >= 6
    for service in protocol.PW_SERVICES:
        assert len(service.methods) > 0

    transfer = protocol.PW_SERVICES["pw.transfer.Transfer"]
    assert {method.name for method in transfer.methods} == {"Read", "Write", "GetResourceStatus"}
    assert transfer.methods["Read"].type.name == "BIDIRECTIONAL_STREAMING"
    assert transfer.methods["Write"].type.name == "BIDIRECTIONAL_STREAMING"
    assert transfer.methods["GetResourceStatus"].type.name == "UNARY"


def test_pw_transfer_service_resolves_through_daemon_rpc_client() -> None:
    """Verify the daemon's generated Pigweed client can resolve upstream pw_transfer methods."""
    from pw_rpc import callback_client
    from pw_rpc.client import Client as PwRpcClient
    from pw_rpc.descriptors import Channel as PwRpcChannel

    client = PwRpcClient(
        cast(Any, callback_client).Impl(),
        [PwRpcChannel(protocol.HDLC_ADDRESS_RPC, lambda _packet: None)],
        protocol.PW_SERVICES,
    )
    transfer_service = client.channel(protocol.HDLC_ADDRESS_RPC).rpcs.pw.transfer.Transfer
    assert callable(transfer_service.Read.invoke)
    assert callable(transfer_service.Write.invoke)
    assert callable(transfer_service.GetResourceStatus.invoke)


def test_operation_specs_multidimensional_integrity() -> None:
    """Verify that every operation has a canonical OperationSpec matching Protobuf descriptors."""
    assert len(protocol.OPERATIONS) >= 30
    for spec in protocol.OPERATIONS:
        assert protocol.get_operation(spec.name) is spec
        if spec.service_id is not None and spec.method_id is not None:
            assert protocol.get_operation((spec.service_id, spec.method_id)) is spec
        if spec.request_type not in (None, mcu_wire_pb2.SubscribeRequest, mcu_wire_pb2.Empty):
            assert spec.pb_class is not None
            assert spec.pb_class is spec.request_type
            msg_spec = protocol.get_operation(spec.pb_class.DESCRIPTOR.name)
            assert msg_spec is not None
            assert msg_spec.pb_class is spec.pb_class
        elif spec.request_type in (mcu_wire_pb2.SubscribeRequest, mcu_wire_pb2.Empty):
            assert spec.pb_class is None


def test_daemon_metric_registry_matches_protobuf_field_options() -> None:
    fields = mcu_wire_pb2.DaemonMetrics.DESCRIPTOR.fields_by_name
    annotated_fields = {
        field.name
        for field in fields.values()
        if field.GetOptions().HasExtension(config_schema_pb2.telemetry_metric)
        and field.GetOptions().Extensions[config_schema_pb2.telemetry_metric].prometheus_name
    }
    specs_by_field = {spec.source_field: spec for spec in protocol.METRICS}
    line_protocol_specs = protocol.LINE_PROTOCOL_FIELDS
    line_protocol_fields = {
        field.name
        for field in fields.values()
        if field.GetOptions().HasExtension(config_schema_pb2.telemetry_metric)
        and field.GetOptions().Extensions[config_schema_pb2.telemetry_metric].line_protocol_name
    }
    ordered_line_fields = sorted(
        (
            field
            for field in fields.values()
            if field.GetOptions().HasExtension(config_schema_pb2.telemetry_metric)
            and field.GetOptions().Extensions[config_schema_pb2.telemetry_metric].line_protocol_name
        ),
        key=lambda field: field.GetOptions().Extensions[config_schema_pb2.telemetry_metric].line_protocol_order,
    )
    line_protocol_kinds = {
        config_schema_pb2.LINE_PROTOCOL_FIELD_KIND_INTEGER: protocol.LineProtocolFieldKind.INTEGER,
        config_schema_pb2.LINE_PROTOCOL_FIELD_KIND_FLOAT: protocol.LineProtocolFieldKind.FLOAT,
        config_schema_pb2.LINE_PROTOCOL_FIELD_KIND_BOOLEAN: protocol.LineProtocolFieldKind.BOOLEAN,
    }

    assert set(specs_by_field) == annotated_fields
    assert len(specs_by_field) == len(protocol.METRICS)
    assert specs_by_field["cloud_dropped_messages"].kind is protocol.MetricKind.COUNTER
    assert specs_by_field["retries"].value_field == "count"
    assert specs_by_field["retries"].labels[0].field == "component"
    assert specs_by_field["serial_latency_ms"].metric_name == specs_by_field["rpc_latency_ms"].metric_name
    assert specs_by_field["serial_latency_ms"].labels[0].static_value == "serial"
    assert specs_by_field["rpc_latency_ms"].labels[0].static_value == "rpc"
    assert {spec.source_field for spec in line_protocol_specs} == line_protocol_fields
    assert [(spec.source_field, spec.field_name) for spec in line_protocol_specs] == [
        (
            field.name,
            field.GetOptions().Extensions[config_schema_pb2.telemetry_metric].line_protocol_name,
        )
        for field in ordered_line_fields
    ]
    assert [spec.kind for spec in line_protocol_specs] == [
        line_protocol_kinds[field.GetOptions().Extensions[config_schema_pb2.telemetry_metric].line_protocol_kind]
        for field in ordered_line_fields
    ]


def test_runtime_config_defaults_reference_canonical_metadata() -> None:
    fields = config_schema_pb2.RuntimeConfig.DESCRIPTOR.fields_by_name
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
        assert options.HasExtension(config_schema_pb2.config_default_ref)
        assert options.Extensions[config_schema_pb2.config_default_ref] == reference
        assert not options.HasExtension(config_schema_pb2.config_default)

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
    fields = mcu_wire_pb2.SpiConfig.DESCRIPTOR.fields_by_name
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
        mcu_wire_pb2.SpiDataMode.Name(mcu_wire_pb2.SpiDataMode.SPI_DATA_MODE_0),
        mcu_wire_pb2.SpiDataMode.Name(mcu_wire_pb2.SpiDataMode.SPI_DATA_MODE_1),
        mcu_wire_pb2.SpiDataMode.Name(mcu_wire_pb2.SpiDataMode.SPI_DATA_MODE_2),
        mcu_wire_pb2.SpiDataMode.Name(mcu_wire_pb2.SpiDataMode.SPI_DATA_MODE_3),
    ],
)
def test_spi_data_modes_round_trip(data_mode_name: str) -> None:
    data_mode = mcu_wire_pb2.SpiDataMode.Value(data_mode_name)
    restored = mcu_wire_pb2.SpiConfig.FromString(mcu_wire_pb2.SpiConfig(data_mode=data_mode).SerializeToString())
    assert restored.data_mode == data_mode


def test_rpc_envelope_strong_typing() -> None:
    """SIL-2: Verify Pigweed RpcPacket fields and roundtrip serialization."""
    from pw_rpc.internal.packet_pb2 import PacketType, RpcPacket

    pkt_fields = RpcPacket.DESCRIPTOR.fields_by_name
    type_field = pkt_fields["type"]
    assert type_field.type == FieldDescriptor.TYPE_ENUM
    assert type_field.enum_type is not None
    assert type_field.enum_type.name == "PacketType"

    # Roundtrip test
    packet = RpcPacket(
        type=PacketType.REQUEST,
        channel_id=1,
        service_id=0x1234,
        method_id=0x5678,
        call_id=10,
        payload=b"test_payload",
    )
    serialized = packet.SerializeToString()

    restored = RpcPacket()
    restored.ParseFromString(serialized)

    assert restored.type == PacketType.REQUEST
    assert restored.channel_id == 1
    assert restored.service_id == 0x1234
    assert restored.method_id == 0x5678
    assert restored.call_id == 10
    assert restored.payload == b"test_payload"


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
    from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
    from mcubridge.services.runtime import BridgeService

    mcu_handlers = {op.name: op.mcu_handler for op in protocol.OPERATIONS if op.mcu_handler}
    assert len(mcu_handlers) == 16
    for name, handler_path in mcu_handlers.items():
        assert isinstance(name, str)
        root_attr = handler_path.split(".", 1)[0]
        assert hasattr(BridgeService, root_attr) or root_attr in ("handshake", "gpio", "clock_sync")
        assert callable(operator.attrgetter(root_attr))

    assert len(protocol.RPC_SPECS) == 25
    from mcubridge.services.runtime import BridgeService

    def _lookup_proto_type(name: str) -> Any:
        for m in (cloud_gateway_pb2, mcu_wire_pb2, config_schema_pb2):
            if hasattr(m, name):
                return getattr(m, name)
        raise AttributeError(f"Protobuf type {name} not found")

    service_desc = next(
        dep.services_by_name["LocalBridge"]
        for dep in [cloud_gateway_pb2.DESCRIPTOR, *cloud_gateway_pb2.DESCRIPTOR.dependencies]
        if "LocalBridge" in dep.services_by_name
    )
    unary_methods = {m.name: m for m in service_desc.methods if not m.client_streaming and not m.server_streaming}
    assert len(unary_methods) == 25

    for spec in protocol.RPC_SPECS:
        method_name = spec.rpc_name or spec.name
        assert method_name in unary_methods
        method_desc = unary_methods[method_name]
        assert spec.request_type is _lookup_proto_type(method_desc.input_type.name)
        assert spec.response_type is _lookup_proto_type(method_desc.output_type.name)
        assert spec.handler_name is not None
        if spec.is_custom_handler:
            assert hasattr(BridgeService, spec.handler_name)
            assert callable(getattr(BridgeService, spec.handler_name))

    assert callable(BridgeService.execute_rpc)


def test_hardware_options_completeness_ssot() -> None:
    """SIL-2: Verify all Hardware fields in config_schema.proto are > 0 and C++ headers delegate to SSoT."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    hw_opts = config_schema_pb2.Hardware.DESCRIPTOR.file.GetOptions().Extensions[config_schema_pb2.hardware]
    zero_fields = [f.name for f in config_schema_pb2.Hardware.DESCRIPTOR.fields if getattr(hw_opts, f.name) <= 0]
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
        repo_root / "mcubridge/uci-defaults/99-mcubridge-defaults",
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
