"""Tests for Protobuf-driven MCU dispatch table generation."""

from __future__ import annotations

from typing import Any, Final

import pytest
from google.protobuf import descriptor_pb2

from mcubridge.protocol import mcubridge_pb2 as pb
from tools.protocol import generate

EXPECTED_DISPATCH_NAMES: Final[set[str]] = set(
    "ACK CMD_GET_VERSION CMD_GET_FREE_MEMORY CMD_LINK_SYNC CMD_LINK_RESET "
    "CMD_GET_CAPABILITIES CMD_SET_BAUDRATE CMD_ENTER_BOOTLOADER CMD_XOFF CMD_XON "
    "CMD_SET_PIN_MODE CMD_DIGITAL_WRITE CMD_ANALOG_WRITE CMD_DIGITAL_READ "
    "CMD_ANALOG_READ CMD_PIN_SUBSCRIBE CMD_CONSOLE_WRITE CMD_DATASTORE_GET_RESP "
    "CMD_MAILBOX_PUSH CMD_MAILBOX_READ_RESP CMD_MAILBOX_AVAILABLE_RESP "
    "CMD_FILE_WRITE CMD_FILE_READ CMD_FILE_REMOVE CMD_FILE_READ_RESP "
    "CMD_PROCESS_KILL CMD_PROCESS_RUN_ASYNC_RESP CMD_PROCESS_POLL_RESP "
    "CMD_SPI_BEGIN CMD_SPI_TRANSFER CMD_SPI_END CMD_SPI_SET_CONFIG CMD_CLOCK_SYNC".split()
)


def test_dispatch_table_is_derived_from_protobuf_and_sorted() -> None:
    context = generate.build_protocol_context(pb, "2.8.8")
    entries: list[dict[str, Any]] = context["mcu_dispatch_entries"]

    assert {str(entry["name"]) for entry in entries} == EXPECTED_DISPATCH_NAMES
    values: list[int] = [int(entry["value"]) for entry in entries]
    assert values == sorted(set(values))

    by_name: dict[str, dict[str, Any]] = {str(entry["name"]): entry for entry in entries}
    assert by_name["ACK"]["key"] == "rpc::StatusCode::STATUS_ACK"
    assert by_name["CMD_GET_VERSION"]["key"] == "rpc::CommandId::CMD_GET_VERSION"
    assert by_name["CMD_GET_VERSION"]["expression"] == (
        "&BridgeClass::_dispatchMemberNoPayload<"
        "&BridgeClass::_handleSimpleQuery<rpc_pb_VersionResponse, "
        "rpc::CommandId::CMD_GET_VERSION_RESP, BridgeClass::_fillVersion>, "
        "false, true, true>"
    )
    assert by_name["CMD_MAILBOX_READ_RESP"]["feature_macro"] == "BRIDGE_ENABLE_MAILBOX"
    assert str(by_name["CMD_SPI_TRANSFER"]["expression"]).endswith("rpc_pb_SpiTransfer, false, true, true>")


def test_dispatch_expression_rejects_unknown_adapters_and_missing_payloads() -> None:
    with pytest.raises(ValueError, match="Unknown MCU dispatch adapter"):
        generate.format_mcu_dispatch_expression({"adapter": "unknown", "handler": "_h"}, "CMD_INVALID")
    with pytest.raises(ValueError, match="payload type"):
        generate.format_mcu_dispatch_expression({"adapter": "member_with_msg", "handler": "_h"}, "CMD_INVALID")


def test_removed_command_metadata_is_absent_not_reserved() -> None:
    descriptor_file = descriptor_pb2.FileDescriptorProto()
    descriptor_file.ParseFromString(pb.Command.DESCRIPTOR.file.serialized_pb)
    cmd_opts = next(msg for msg in descriptor_file.message_type if msg.name == "CommandOptions")
    assert {"category", "is_query"}.isdisjoint({f.name for f in cmd_opts.field} | set(cmd_opts.reserved_name))
    reserved_nums = {num for r in cmd_opts.reserved_range for num in range(r.start, r.end)}
    assert {1, 16}.isdisjoint(reserved_nums)
