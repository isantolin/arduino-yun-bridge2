"""Tests for Protobuf-driven MCU dispatch table generation."""

from __future__ import annotations

from typing import Any, Final

from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2
from tools.protocol import generate


def _make_codegen_proto() -> Any:
    class _UnifiedProto:
        def __init__(self) -> None:
            for m in (config_schema_pb2, mcu_wire_pb2, cloud_gateway_pb2):
                for k in dir(m):
                    if not k.startswith("__"):
                        setattr(self, k, getattr(m, k))
            self.DESCRIPTOR = cloud_gateway_pb2.DESCRIPTOR

    return _UnifiedProto()


EXPECTED_OPERATION_NAMES: Final[set[str]] = {
    "rpc.pb.SystemService/GetVersion",
    "rpc.pb.SystemService/GetFreeMemory",
    "rpc.pb.SystemService/LinkSync",
    "rpc.pb.SystemService/LinkReset",
    "rpc.pb.SystemService/GetCapabilities",
    "rpc.pb.SystemService/SetBaudrate",
    "rpc.pb.SystemService/EnterBootloader",
    "rpc.pb.GpioService/SetPinMode",
    "rpc.pb.GpioService/DigitalWrite",
    "rpc.pb.GpioService/AnalogWrite",
    "rpc.pb.GpioService/DigitalRead",
    "rpc.pb.GpioService/AnalogRead",
    "rpc.pb.GpioService/SubscribePins",
    "ConsoleWrite",
    "rpc.pb.DataStoreService/Put",
    "rpc.pb.DataStoreService/Get",
    "rpc.pb.MailboxService/Push",
    "rpc.pb.MailboxService/Read",
    "rpc.pb.MailboxService/Available",
    "rpc.pb.MailboxService/Processed",
    "rpc.pb.FileSystemService/Write",
    "rpc.pb.FileSystemService/Remove",
    "rpc.pb.ProcessService/Kill",
    "rpc.pb.ProcessService/RunAsync",
    "rpc.pb.ProcessService/Poll",
    "rpc.pb.SpiService/Begin",
    "rpc.pb.SpiService/Transfer",
    "rpc.pb.SpiService/End",
    "rpc.pb.SpiService/SetConfig",
    "rpc.pb.ClockSyncService/ClockSync",
}


def test_operations_are_derived_from_protobuf_services() -> None:
    context = generate.build_protocol_context(_make_codegen_proto(), "2.8.8")
    operations: list[dict[str, Any]] = context["operations"]

    op_names = {str(op["name"]) for op in operations}
    assert EXPECTED_OPERATION_NAMES.issubset(op_names)

    by_name: dict[str, dict[str, Any]] = {str(op["name"]): op for op in operations}
    assert by_name["rpc.pb.SystemService/LinkSync"]["pre_sync_allowed"] is True
    assert by_name["rpc.pb.SystemService/LinkSync"]["mcu_handler"] == "handshake.handle_link_sync_resp"
    assert by_name["rpc.pb.SystemService/GetVersion"]["auth_field"] == "system_version"


def test_pigweed_rpc_method_mappings() -> None:
    from mcubridge.protocol import protocol

    assert len(protocol.BY_RPC) > 0
    assert len(protocol.BY_NAME) > 0
    assert len(protocol.PW_SERVICES) >= 8

    for (srv_id, meth_id), spec in protocol.BY_RPC.items():
        assert isinstance(srv_id, int)
        assert srv_id > 0
        assert isinstance(meth_id, int)
        assert meth_id > 0
        assert isinstance(spec.service_name, str)
        assert len(spec.service_name) > 0
        assert isinstance(spec.method_name, str)
        assert len(spec.method_name) > 0

        assert protocol.get_operation((srv_id, meth_id)) is spec
        assert spec.service_id == srv_id
        assert spec.method_id == meth_id
        assert protocol.PW_SERVICES[srv_id].methods[meth_id].id == meth_id

    # Verify key Pigweed RPC service and method mappings via PW_SERVICES and get_operation
    gpio_write = protocol.get_operation("rpc.pb.GpioService/DigitalWrite")
    assert gpio_write is not None
    assert gpio_write.service_name == "rpc.pb.GpioService"
    assert gpio_write.method_name == "DigitalWrite"

    sys_ver = protocol.get_operation("rpc.pb.SystemService/GetVersion")
    assert sys_ver is not None
    assert sys_ver.service_name == "rpc.pb.SystemService"
    assert sys_ver.method_name == "GetVersion"

    ds_put = protocol.get_operation("rpc.pb.DataStoreService/Put")
    assert ds_put is not None
    assert ds_put.service_name == "rpc.pb.DataStoreService"
    assert ds_put.method_name == "Put"
    assert protocol.get_operation(ds_put) is ds_put
    assert protocol.get_operation("rpc.pb.DataStoreService/Put") is ds_put


def test_every_mcu_rpc_method_has_an_operation_spec() -> None:
    """Every MCU-facing Pigweed RPC method must be registered in OPERATIONS and BY_RPC."""
    from mcubridge.protocol import protocol

    descriptors = [cloud_gateway_pb2.DESCRIPTOR, *cloud_gateway_pb2.DESCRIPTOR.dependencies]

    operations = {
        str(op["name"]): op for op in generate.build_protocol_context(_make_codegen_proto(), "2.8.8")["operations"]
    }

    seen_methods: set[str] = set()
    for file_descriptor in descriptors:
        for service in file_descriptor.services_by_name.values():
            if service.name in {"CloudBridge", "LocalBridge"}:
                continue
            for method in service.methods:
                full_key = f"{service.full_name}/{method.name}"
                assert full_key not in seen_methods
                seen_methods.add(full_key)
                assert full_key in operations

                operation = operations[full_key]
                assert operation["service_name"] == service.full_name
                assert operation["method_name"] == method.name
                resolved = protocol.get_operation(full_key)
                assert resolved is not None
                assert resolved.service_name == service.full_name
                assert resolved.method_name == method.name
                assert resolved.service_id is not None
                assert resolved.method_id is not None
                assert protocol.get_operation((resolved.service_id, resolved.method_id)) is resolved

    assert seen_methods, "no MCU-facing Pigweed RPC methods were audited"
