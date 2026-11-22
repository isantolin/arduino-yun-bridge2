"""Regression tests for CI dead-code and Protobuf reachability gates (Bloque 4). [SIL-2]"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.descriptor import FileDescriptor

from tools.audit import audit_test_integrity, codebase_auditor


class _DescriptorPoolLoader(Protocol):
    def AddSerializedFile(self, serialized_file_desc_proto: bytes) -> FileDescriptor: ...


def _build_descriptor_pool_file(file_proto: descriptor_pb2.FileDescriptorProto) -> FileDescriptor:
    """Compile an in-memory FileDescriptorProto into an isolated DescriptorPool."""
    pool = cast(_DescriptorPoolLoader, descriptor_pool.DescriptorPool())
    if "google/protobuf/descriptor.proto" in file_proto.dependency:
        pool.AddSerializedFile(descriptor_pb2.DESCRIPTOR.serialized_pb)
    return pool.AddSerializedFile(file_proto.SerializeToString())


def _add_message_field(
    msg: descriptor_pb2.DescriptorProto,
    *,
    name: str,
    number: int,
    field_type: descriptor_pb2.FieldDescriptorProto.Type.ValueType,
    type_name: str = "",
    label: descriptor_pb2.FieldDescriptorProto.Label.ValueType = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
) -> None:
    field = msg.field.add()
    field.name = name
    field.number = number
    field.label = label
    field.type = field_type
    if type_name:
        field.type_name = type_name


def test_audit_test_only_hooks_catches_client_package_test_only_method(tmp_path: Path) -> None:
    """Rule 5 must scan mcubridge-client-examples/mcubridge_client against client tests."""
    client_mod = tmp_path / "mcubridge-client-examples" / "mcubridge_client" / "topic_helper.py"
    client_test = tmp_path / "mcubridge-client-examples" / "tests" / "test_topic_helper.py"
    client_mod.parent.mkdir(parents=True, exist_ok=True)
    client_test.parent.mkdir(parents=True, exist_ok=True)

    client_mod.write_text(
        "class TopicHelper:\n"
        "    @staticmethod\n"
        "    def matches(pattern: str, topic: str) -> bool:\n"
        "        return pattern == topic\n",
        encoding="utf-8",
    )
    client_test.write_text(
        "def test_matches() -> None:\n    assert TopicHelper.matches('a/b', 'a/b')\n",
        encoding="utf-8",
    )

    findings = audit_test_integrity.audit_test_only_hooks(tmp_path)

    assert any("Rule 5 Violation" in f and "'matches'" in f for f in findings)
    assert any("Rule 5 Violation" in f and "'TopicHelper'" in f for f in findings)


def test_audit_test_only_hooks_catches_daemon_handle_prefix_backdoor(tmp_path: Path) -> None:
    """Blanket 'handle_' prefix exemption is removed; only true dynamic-dispatch prefixes are exempted."""
    prod_mod = tmp_path / "mcubridge" / "mcubridge" / "services" / "legacy.py"
    test_mod = tmp_path / "mcubridge" / "tests" / "test_legacy.py"
    prod_mod.parent.mkdir(parents=True, exist_ok=True)
    test_mod.parent.mkdir(parents=True, exist_ok=True)

    prod_mod.write_text(
        "class LegacyService:\n"
        "    def handle_legacy_backdoor(self) -> int:\n"
        "        return 1\n"
        "    def ubus_handle_status(self) -> int:\n"
        "        return 2\n"
        "    def on_enter_ready(self) -> None:\n"
        "        return None\n"
        "    def on_mcu_ping(self) -> None:\n"
        "        return None\n"
        "    def _on_mcu_sync(self) -> None:\n"
        "        return None\n"
        "    def on_exit_ready(self) -> None:\n"
        "        return None\n"
        "\n"
        "def make_service() -> LegacyService:\n"
        "    return LegacyService()\n",
        encoding="utf-8",
    )
    test_mod.write_text(
        "def test_legacy() -> None:\n"
        "    svc = LegacyService()\n"
        "    assert svc.handle_legacy_backdoor() == 1\n"
        "    assert svc.ubus_handle_status() == 2\n"
        "    svc.on_enter_ready()\n"
        "    svc.on_exit_ready()\n"
        "    svc.on_mcu_ping()\n"
        "    svc._on_mcu_sync()\n",
        encoding="utf-8",
    )

    findings = audit_test_integrity.audit_test_only_hooks(tmp_path)

    assert len(findings) == 1
    assert "'handle_legacy_backdoor'" in findings[0]
    assert "Rule 5 Violation" in findings[0]


def test_audit_test_only_hooks_allows_example_and_script_consumers(tmp_path: Path) -> None:
    """Helpers called from mcubridge-client-examples/examples or mcubridge/scripts are production consumers."""
    client_mod = tmp_path / "mcubridge-client-examples" / "mcubridge_client" / "sdk.py"
    example_mod = tmp_path / "mcubridge-client-examples" / "examples" / "demo_example.py"
    client_test = tmp_path / "mcubridge-client-examples" / "tests" / "test_sdk.py"
    daemon_mod = tmp_path / "mcubridge" / "mcubridge" / "daemon_util.py"
    script_mod = tmp_path / "mcubridge" / "scripts" / "tool.py"
    daemon_test = tmp_path / "mcubridge" / "tests" / "test_daemon_util.py"

    for path in (client_mod, example_mod, client_test, daemon_mod, script_mod, daemon_test):
        path.parent.mkdir(parents=True, exist_ok=True)

    client_mod.write_text(
        "class ClientSdk:\n    def invoke_public_api(self) -> str:\n        return 'ok'\n",
        encoding="utf-8",
    )
    example_mod.write_text(
        "def main() -> str:\n    return ClientSdk().invoke_public_api()\n",
        encoding="utf-8",
    )
    client_test.write_text(
        "def test_sdk() -> None:\n    assert ClientSdk().invoke_public_api() == 'ok'\n",
        encoding="utf-8",
    )
    daemon_mod.write_text(
        "def script_consumer_helper() -> int:\n    return 7\n",
        encoding="utf-8",
    )
    script_mod.write_text(
        "def main() -> int:\n    return script_consumer_helper()\n",
        encoding="utf-8",
    )
    daemon_test.write_text(
        "def test_daemon_util() -> None:\n    assert script_consumer_helper() == 7\n",
        encoding="utf-8",
    )

    assert audit_test_integrity.audit_test_only_hooks(tmp_path) == []


def test_audit_test_only_hooks_ignores_docstrings_and_test_comments(tmp_path: Path) -> None:
    """Self-mentions in docstrings must not hide hooks, and prose in test comments must not false-positive."""
    prod_mod = tmp_path / "mcubridge" / "mcubridge" / "backdoor.py"
    test_mod = tmp_path / "mcubridge" / "tests" / "test_backdoor.py"
    prod_mod.parent.mkdir(parents=True, exist_ok=True)
    test_mod.parent.mkdir(parents=True, exist_ok=True)

    prod_mod.write_text(
        "def secret_backdoor() -> int:\n"
        '    """Test hook secret_backdoor mentions its own name in a docstring."""\n'
        "    return 99\n"
        "\n"
        "def comment_only_word() -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )
    test_mod.write_text(
        "def test_backdoor() -> None:\n"
        "    # comment_only_word appears only in this comment, never called in code.\n"
        "    assert secret_backdoor() == 99\n",
        encoding="utf-8",
    )

    findings = audit_test_integrity.audit_test_only_hooks(tmp_path)

    assert len(findings) == 1
    assert "'secret_backdoor'" in findings[0]


def test_audit_test_only_hooks_catches_unused_production_class(tmp_path: Path) -> None:
    """Rule 5 detects production classes referenced exclusively from tests."""
    prod_mod = tmp_path / "mcubridge" / "mcubridge" / "models.py"
    test_mod = tmp_path / "mcubridge" / "tests" / "test_models.py"
    prod_mod.parent.mkdir(parents=True, exist_ok=True)
    test_mod.parent.mkdir(parents=True, exist_ok=True)

    prod_mod.write_text(
        "class TestOnlyModel:\n    value: int = 1\n",
        encoding="utf-8",
    )
    test_mod.write_text(
        "def test_model() -> None:\n    assert TestOnlyModel.value == 1\n",
        encoding="utf-8",
    )

    findings = audit_test_integrity.audit_test_only_hooks(tmp_path)

    assert len(findings) == 1
    assert "Production class 'TestOnlyModel'" in findings[0]


def test_allowlist_py_helpers_pruned_and_minimal() -> None:
    """Obsolete allowlist entries with real production callers must stay pruned."""
    pruned_entries = {
        "emit_bridge_snapshot",
        "flush_cloud_spool",
        "reject_cloud",
        "run_cloud",
        "transfer_read",
        "transfer_write",
    }
    assert pruned_entries.isdisjoint(audit_test_integrity.ALLOWLIST_PY_HELPERS)
    assert len(audit_test_integrity.ALLOWLIST_PY_HELPERS) <= 5


def test_proto_reachability_catches_isolated_dead_message_and_subgraph() -> None:
    """Reachability catches isolated dead messages and dead subgraphs while allowing transitive live deps."""
    file_proto = descriptor_pb2.FileDescriptorProto(name="synthetic_cloud.proto", package="demo.pb", syntax="proto3")

    live_enum = file_proto.enum_type.add(name="LiveMode")
    live_enum.value.add(name="LIVE_MODE_UNSPECIFIED", number=0)
    live_enum.value.add(name="LIVE_MODE_FAST", number=1)

    dead_sub_enum = file_proto.enum_type.add(name="DeadSubEnum")
    dead_sub_enum.value.add(name="DEAD_SUB_ENUM_UNSPECIFIED", number=0)
    dead_sub_enum.value.add(name="DEAD_SUB_ENUM_ACTIVE", number=1)

    dead_isolated_enum = file_proto.enum_type.add(name="DeadIsolatedEnum")
    dead_isolated_enum.value.add(name="DEAD_ISOLATED_ENUM_UNSPECIFIED", number=0)
    dead_isolated_enum.value.add(name="DEAD_ISOLATED_ENUM_ONLY", number=1)

    live_inner = file_proto.message_type.add(name="LiveInner")
    _add_message_field(live_inner, name="note", number=1, field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING)

    live_map_val = file_proto.message_type.add(name="LiveMapValue")
    _add_message_field(live_map_val, name="count", number=1, field_type=descriptor_pb2.FieldDescriptorProto.TYPE_UINT32)

    live_outer = file_proto.message_type.add(name="LiveOuter")
    _add_message_field(
        live_outer,
        name="inner",
        number=1,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".demo.pb.LiveInner",
    )
    _add_message_field(
        live_outer,
        name="mode",
        number=2,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
        type_name=".demo.pb.LiveMode",
    )
    map_entry = live_outer.nested_type.add(name="ItemsEntry")
    map_entry.options.map_entry = True
    _add_message_field(map_entry, name="key", number=1, field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    _add_message_field(
        map_entry,
        name="value",
        number=2,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".demo.pb.LiveMapValue",
    )
    _add_message_field(
        live_outer,
        name="items",
        number=3,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".demo.pb.LiveOuter.ItemsEntry",
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
    )

    dead_inner = file_proto.message_type.add(name="DeadInner")
    _add_message_field(dead_inner, name="seq", number=1, field_type=descriptor_pb2.FieldDescriptorProto.TYPE_UINT32)

    dead_outer = file_proto.message_type.add(name="DeadOuter")
    _add_message_field(
        dead_outer,
        name="inner",
        number=1,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".demo.pb.DeadInner",
    )
    _add_message_field(
        dead_outer,
        name="kind",
        number=2,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
        type_name=".demo.pb.DeadSubEnum",
    )

    dead_isolated = file_proto.message_type.add(name="DeadIsolated")
    _add_message_field(dead_isolated, name="raw", number=1, field_type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES)

    file_desc = _build_descriptor_pool_file(file_proto)
    findings = codebase_auditor.audit_proto_descriptors_reachability([file_desc], {"LiveOuter"})

    assert findings == [
        "Unused Protobuf message: synthetic_cloud.proto:DeadInner",
        "Unused Protobuf message: synthetic_cloud.proto:DeadIsolated",
        "Unused Protobuf message: synthetic_cloud.proto:DeadOuter",
        "Unused Protobuf enum: synthetic_cloud.proto:DeadIsolatedEnum",
        "Unused Protobuf enum: synthetic_cloud.proto:DeadSubEnum",
    ]


def test_proto_reachability_excludes_test_generated_comment_and_audit_refs(tmp_path: Path) -> None:
    """Only real production, C++ (including .cc and nanopb), and codegen refs make proto types live."""
    py_prod = tmp_path / "mcubridge" / "mcubridge" / "app.py"
    gw_cpp = tmp_path / "mcubridge-gateway-cpp" / "src" / "gateway.cc"
    mcu_cpp = tmp_path / "mcubridge-library-arduino" / "src" / "Bridge.cpp"
    py_test = tmp_path / "mcubridge" / "tests" / "test_proto_dead.py"
    gw_test = tmp_path / "mcubridge-gateway-cpp" / "tests" / "test_gw.cc"
    gen_pb2 = tmp_path / "mcubridge" / "mcubridge" / "protocol" / "synthetic_pb2.py"
    audit_file = tmp_path / "tools" / "audit" / "custom_audit.py"

    for path in (py_prod, gw_cpp, mcu_cpp, py_test, gw_test, gen_pb2, audit_file):
        path.parent.mkdir(parents=True, exist_ok=True)

    py_prod.write_text("def send() -> object:\n    return LivePyMsg()\n", encoding="utf-8")
    gw_cpp.write_text(
        "// DeadCommentMsg should be stripped with comments\n"
        "void dispatch() { pwpb::LiveGatewayMsg::Message msg{}; }\n",
        encoding="utf-8",
    )
    mcu_cpp.write_text("rpc_pb_LiveArduinoMsg msg = rpc_pb_LiveArduinoMsg_init_zero;\n", encoding="utf-8")
    py_test.write_text("def test_dead() -> None:\n    assert TestOnlyProtoMsg() is not None\n", encoding="utf-8")
    gw_test.write_text("void test_gw() { CppTestOnlyProtoMsg msg{}; }\n", encoding="utf-8")
    gen_pb2.write_text("class GeneratedOnlyMsg:\n    pass\n", encoding="utf-8")
    audit_file.write_text("CHECKED = 'AuditOnlyMsg'\nAuditOnlyMsg = object\n", encoding="utf-8")

    file_proto = descriptor_pb2.FileDescriptorProto(name="boundary.proto", package="rpc.pb", syntax="proto3")
    for msg_name in (
        "LivePyMsg",
        "LiveGatewayMsg",
        "LiveArduinoMsg",
        "DeadCommentMsg",
        "TestOnlyProtoMsg",
        "CppTestOnlyProtoMsg",
        "GeneratedOnlyMsg",
        "AuditOnlyMsg",
    ):
        file_proto.message_type.add(name=msg_name)

    file_desc = _build_descriptor_pool_file(file_proto)
    prod_ids = codebase_auditor.collect_production_proto_identifiers(tmp_path)
    findings = codebase_auditor.audit_proto_descriptors_reachability([file_desc], prod_ids)

    assert findings == [
        "Unused Protobuf message: boundary.proto:AuditOnlyMsg",
        "Unused Protobuf message: boundary.proto:CppTestOnlyProtoMsg",
        "Unused Protobuf message: boundary.proto:DeadCommentMsg",
        "Unused Protobuf message: boundary.proto:GeneratedOnlyMsg",
        "Unused Protobuf message: boundary.proto:TestOnlyProtoMsg",
    ]


def test_proto_reachability_excludes_google_protobuf_option_extensions() -> None:
    """Messages and enums used exclusively as google.protobuf.*Options extensions are excluded."""
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="ext_schema.proto",
        package="demo.pb",
        syntax="proto3",
        dependency=["google/protobuf/descriptor.proto"],
    )

    ext_enum = file_proto.enum_type.add(name="OptionKind")
    ext_enum.value.add(name="OPTION_KIND_UNSPECIFIED", number=0)
    ext_enum.value.add(name="OPTION_KIND_A", number=1)

    ext_nested = file_proto.message_type.add(name="OptionLabel")
    _add_message_field(ext_nested, name="name", number=1, field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING)

    ext_msg = file_proto.message_type.add(name="SchemaMetadata")
    _add_message_field(
        ext_msg,
        name="kind",
        number=1,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
        type_name=".demo.pb.OptionKind",
    )
    _add_message_field(
        ext_msg,
        name="label",
        number=2,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".demo.pb.OptionLabel",
    )

    ext_field = file_proto.extension.add()
    ext_field.name = "schema_metadata"
    ext_field.number = 50001
    ext_field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    ext_field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    ext_field.type_name = ".demo.pb.SchemaMetadata"
    ext_field.extendee = ".google.protobuf.FileOptions"

    file_proto.message_type.add(name="DeadPayload")

    file_desc = _build_descriptor_pool_file(file_proto)
    findings = codebase_auditor.audit_proto_descriptors_reachability([file_desc], set())

    assert findings == ["Unused Protobuf message: ext_schema.proto:DeadPayload"]


def test_audit_proto_usage_clean_on_repository() -> None:
    """All canonical .proto schemas must have zero dead or unreachable definitions."""
    assert codebase_auditor.audit_proto_usage() == []
