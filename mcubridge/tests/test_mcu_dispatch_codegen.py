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
    "CMD_DATASTORE_GET_RESP",
    "CMD_MAILBOX_PUSH",
    "CMD_MAILBOX_READ_RESP",
    "CMD_MAILBOX_AVAILABLE_RESP",
    "CMD_FILE_WRITE",
    "CMD_FILE_READ",
    "CMD_FILE_REMOVE",
    "CMD_FILE_READ_RESP",
    "CMD_PROCESS_KILL",
    "CMD_PROCESS_RUN_ASYNC_RESP",
    "CMD_PROCESS_POLL_RESP",
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
    assert by_name["CMD_GET_VERSION"]["response_command"] == "CMD_GET_VERSION_RESP"
    assert by_name["CMD_GET_VERSION"]["auth_field"] == "system_version"
    assert by_name["CMD_MAILBOX_READ_RESP"]["pb_message"] == "MailboxReadResponse"
    assert by_name["CMD_MAILBOX_READ_RESP"]["request_command"] == "CMD_MAILBOX_READ"


def test_pigweed_rpc_method_mappings() -> None:
    from mcubridge.protocol import protocol

    assert len(protocol.REGISTRY.by_rpc) > 0
    assert len(protocol.REGISTRY.by_command_id) > 0

    for (srv_id, meth_id), spec in protocol.REGISTRY.by_rpc.items():
        assert isinstance(srv_id, int)
        assert srv_id > 0
        assert isinstance(meth_id, int)
        assert meth_id > 0
        assert isinstance(spec.service_name, str)
        assert len(spec.service_name) > 0
        assert isinstance(spec.method_name, str)
        assert len(spec.method_name) > 0

        assert protocol.REGISTRY.get((srv_id, meth_id)) is spec
        assert spec.service_id == srv_id
        assert spec.method_id == meth_id

    # Verify key Pigweed RPC service and method mappings via REGISTRY
    gpio_write = protocol.REGISTRY.get(protocol.Command.CMD_DIGITAL_WRITE.value)
    assert gpio_write is not None
    assert gpio_write.service_name == "rpc.pb.GpioService"
    assert gpio_write.method_name == "DigitalWrite"

    sys_ver = protocol.REGISTRY.get(protocol.Command.CMD_GET_VERSION.value)
    assert sys_ver is not None
    assert sys_ver.service_name == "rpc.pb.SystemService"
    assert sys_ver.method_name == "GetVersion"

    ds_put = protocol.REGISTRY.get(protocol.Command.CMD_DATASTORE_PUT.value)
    assert ds_put is not None
    assert ds_put.service_name == "rpc.pb.DataStoreService"
    assert ds_put.method_name == "Put"


def test_removed_command_metadata_is_absent_not_reserved() -> None:
    descriptor_file = descriptor_pb2.FileDescriptorProto()
    descriptor_file.ParseFromString(pb.Command.DESCRIPTOR.file.serialized_pb)
    cmd_opts = next(msg for msg in descriptor_file.message_type if msg.name == "CommandOptions")
    assert {"category", "is_query"}.isdisjoint({f.name for f in cmd_opts.field} | set(cmd_opts.reserved_name))
    reserved_nums = {num for r in cmd_opts.reserved_range for num in range(r.start, r.end)}
    assert {1, 16}.isdisjoint(reserved_nums)
