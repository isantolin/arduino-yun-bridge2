"""Tests for Protobuf-driven MCU dispatch table generation."""

from __future__ import annotations

from typing import Any, Final

from google.protobuf import descriptor_pb2

from mcubridge.protocol import mcubridge_pb2 as pb
from tools.protocol import generate

EXPECTED_DISPATCH_NAMES: Final[set[str]] = {
    "CMD_GET_VERSION",
    "CMD_GET_FREE_MEMORY",
    "CMD_LINK_SYNC",
    "CMD_LINK_RESET",
    "CMD_GET_CAPABILITIES",
    "CMD_SET_BAUDRATE",
    "CMD_ENTER_BOOTLOADER",
    "CMD_XOFF",
    "CMD_XON",
    "CMD_SET_PIN_MODE",
    "CMD_DIGITAL_WRITE",
    "CMD_ANALOG_WRITE",
    "CMD_DIGITAL_READ",
    "CMD_ANALOG_READ",
    "CMD_PIN_SUBSCRIBE",
    "CMD_CONSOLE_WRITE",
    "CMD_DATASTORE_GET",
    "CMD_MAILBOX_PUSH",
    "CMD_MAILBOX_READ",
    "CMD_MAILBOX_AVAILABLE",
    "CMD_FILE_WRITE",
    "CMD_FILE_READ",
    "CMD_FILE_REMOVE",
    "CMD_PROCESS_KILL",
    "CMD_PROCESS_RUN_ASYNC",
    "CMD_PROCESS_POLL",
    "CMD_SPI_BEGIN",
    "CMD_SPI_TRANSFER",
    "CMD_SPI_END",
    "CMD_SPI_SET_CONFIG",
    "CMD_CLOCK_SYNC",
}


def test_commands_are_derived_from_protobuf_and_sorted() -> None:
    context = generate.build_protocol_context(pb, "2.8.8")
    commands: list[dict[str, Any]] = context["commands"]

    command_names = {str(c["name"]) for c in commands}
    assert EXPECTED_DISPATCH_NAMES.issubset(command_names)

    values: list[int] = [int(c["value"]) for c in commands]
    assert values == sorted(set(values))

    by_name: dict[str, dict[str, Any]] = {str(c["name"]): c for c in commands}
    assert by_name["CMD_LINK_SYNC"]["response_command"] == "CMD_LINK_SYNC_RESP"
    assert by_name["CMD_GET_VERSION"]["auth_field"] == "system_version"
    assert by_name["CMD_LINK_SYNC_RESP"]["pb_message"] == "LinkSync"
    assert by_name["CMD_LINK_SYNC_RESP"]["request_command"] == "CMD_LINK_SYNC"


def test_pigweed_rpc_method_mappings() -> None:
    from mcubridge.protocol import protocol

    assert len(protocol.BY_RPC) > 0
    assert len(protocol.BY_COMMAND_ID) > 0
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
    gpio_write = protocol.get_operation(protocol.Command.CMD_DIGITAL_WRITE.value)
    assert gpio_write is not None
    assert gpio_write.service_name == "rpc.pb.GpioService"
    assert gpio_write.method_name == "DigitalWrite"

    sys_ver = protocol.get_operation(protocol.Command.CMD_GET_VERSION.value)
    assert sys_ver is not None
    assert sys_ver.service_name == "rpc.pb.SystemService"
    assert sys_ver.method_name == "GetVersion"

    ds_put = protocol.get_operation(protocol.Command.CMD_DATASTORE_PUT.value)
    assert ds_put is not None
    assert ds_put.service_name == "rpc.pb.DataStoreService"
    assert ds_put.method_name == "Put"


def test_every_mcu_rpc_method_has_an_explicit_command_binding() -> None:
    """Every MCU-facing Pigweed RPC method must be bound to exactly one CMD_* ID."""
    from mcubridge.protocol import protocol

    descriptors = [pb.DESCRIPTOR, *pb.DESCRIPTOR.dependencies]
    command_names = {value.name for value in pb.Command.DESCRIPTOR.values}
    operations = {str(op["name"]): op for op in generate.build_protocol_context(pb, "2.8.8")["operations"]}

    seen_commands: set[str] = set()
    for file_descriptor in descriptors:
        for service in file_descriptor.services_by_name.values():
            if service.name in {"CloudBridge", "LocalBridge"}:
                continue
            for method in service.methods:
                options = method.GetOptions()
                assert options.HasExtension(pb.rpc_command), f"{service.full_name}.{method.name}"
                command_name = options.Extensions[pb.rpc_command]
                assert command_name in command_names
                assert command_name not in seen_commands, f"duplicate binding for {command_name}"
                seen_commands.add(command_name)

                operation = operations[command_name]
                assert operation["service_name"] == service.full_name
                assert operation["method_name"] == method.name
                resolved = protocol.get_operation(protocol.Command[command_name].value)
                assert resolved is not None
                assert resolved.service_name == service.full_name
                assert resolved.method_name == method.name

    assert seen_commands, "no MCU-facing Pigweed RPC methods were audited"


def test_removed_command_metadata_is_absent_not_reserved() -> None:
    descriptor_file = descriptor_pb2.FileDescriptorProto()
    descriptor_file.ParseFromString(pb.Command.DESCRIPTOR.file.serialized_pb)
    cmd_opts = next(msg for msg in descriptor_file.message_type if msg.name == "CommandOptions")
    assert {"category", "is_query"}.isdisjoint({f.name for f in cmd_opts.field} | set(cmd_opts.reserved_name))
    reserved_nums = {num for r in cmd_opts.reserved_range for num in range(r.start, r.end)}
    assert {1, 16}.isdisjoint(reserved_nums)
