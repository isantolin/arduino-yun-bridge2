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

    assert len(protocol.COMMAND_TO_RPC) > 0
    assert len(protocol.RPC_METHOD_TO_SPEC) > 0
    assert len(protocol.RPC_TO_COMMAND) > 0

    for cmd, spec in protocol.COMMAND_TO_RPC.items():
        assert isinstance(cmd, int)
        assert isinstance(spec.service_id, int)
        assert spec.service_id > 0
        assert isinstance(spec.method_id, int)
        assert spec.method_id > 0
        assert isinstance(spec.service_name, str)
        assert len(spec.service_name) > 0
        assert isinstance(spec.method_name, str)
        assert len(spec.method_name) > 0

        key = (spec.service_id, spec.method_id)
        assert key in protocol.RPC_METHOD_TO_SPEC
        matched_spec = protocol.RPC_METHOD_TO_SPEC[key]
        assert matched_spec.service_id == spec.service_id
        assert matched_spec.method_id == spec.method_id
        assert matched_spec.service_name == spec.service_name
        assert matched_spec.method_name == spec.method_name

    # Verify key Pigweed RPC service and method mappings
    gpio_write = protocol.COMMAND_TO_RPC[protocol.Command.CMD_DIGITAL_WRITE.value]
    assert gpio_write.service_name == "bridge.GpioService"
    assert gpio_write.method_name == "DigitalWrite"

    sys_ver = protocol.COMMAND_TO_RPC[protocol.Command.CMD_GET_VERSION.value]
    assert sys_ver.service_name == "bridge.SystemService"
    assert sys_ver.method_name == "GetVersion"

    ds_put = protocol.COMMAND_TO_RPC[protocol.Command.CMD_DATASTORE_PUT.value]
    assert ds_put.service_name == "bridge.DataStoreService"
    assert ds_put.method_name == "Put"


def test_removed_command_metadata_is_absent_not_reserved() -> None:
    descriptor_file = descriptor_pb2.FileDescriptorProto()
    descriptor_file.ParseFromString(pb.Command.DESCRIPTOR.file.serialized_pb)
    cmd_opts = next(msg for msg in descriptor_file.message_type if msg.name == "CommandOptions")
    assert {"category", "is_query"}.isdisjoint({f.name for f in cmd_opts.field} | set(cmd_opts.reserved_name))
    reserved_nums = {num for r in cmd_opts.reserved_range for num in range(r.start, r.end)}
    assert {1, 16}.isdisjoint(reserved_nums)
