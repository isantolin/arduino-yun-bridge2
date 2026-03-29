"""Tests for frame_debug tool."""

from __future__ import annotations

import binascii
import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import Command, Status, UINT8_MASK
from tests.test_constants import TEST_BROKEN_CRC  # noqa: E402

from tools import frame_debug  # noqa: E402


def test_resolve_command_hex() -> None:
    assert frame_debug._resolve_command(f"0x{Command.CMD_LINK_RESET.value:02X}") == Command.CMD_LINK_RESET.value
    # Use lowercase 0x to match frame_debug.py startswith if upper() was missing
    assert frame_debug._resolve_command(f"0x{UINT8_MASK:02X}") == UINT8_MASK
    assert frame_debug._resolve_command("10") == 10  # Just an integer


def test_resolve_command_name() -> None:
    assert frame_debug._resolve_command("CMD_GET_VERSION") == Command.CMD_GET_VERSION.value
    assert frame_debug._resolve_command("CMD_GET_FREE_MEMORY") == Command.CMD_GET_FREE_MEMORY.value
    # Case insensitive
    assert frame_debug._resolve_command("cmd_get_version") == Command.CMD_GET_VERSION.value


def test_resolve_command_invalid() -> None:
    with pytest.raises(ValueError, match="command may not be empty"):
        frame_debug._resolve_command("")

    with pytest.raises(ValueError, match="Unknown command"):
        frame_debug._resolve_command("INVALID_CMD")


def test_parse_payload() -> None:
    assert frame_debug._parse_payload(None) == b""
    assert frame_debug._parse_payload("") == b""
    assert frame_debug._parse_payload("010203") == bytes([1, 2, 3])
    assert frame_debug._parse_payload(f"0x{1:02X}{2:02X}") == bytes([1, 2])
    assert frame_debug._parse_payload("01 02 03") == bytes([1, 2, 3])


def test_parse_payload_invalid() -> None:
    # binascii.unhexlify raises binascii.Error: Odd-length string
    with pytest.raises(ValueError, match="Odd-length string"):
        frame_debug._parse_payload("123")

    with pytest.raises(ValueError, match="Invalid hex payload"):
        frame_debug._parse_payload("ZZ")


def test_name_for_command() -> None:
    assert frame_debug._name_for_command(Command.CMD_GET_VERSION.value) == "CMD_GET_VERSION"
    # Keep testing Status resolution
    assert frame_debug._name_for_command(Status.ACK.value) == "ACK"
    assert frame_debug._name_for_command(UINT8_MASK) == f"UNKNOWN(0x{UINT8_MASK:02X})"


def test_snapshot_render() -> None:
    snapshot = frame_debug.FrameDebugSnapshot(
        command_id=Command.CMD_GET_VERSION.value,
        command_name="CMD_GET_VERSION",
        payload_length=5,
        crc=TEST_BROKEN_CRC,
        raw_length=10,
        cobs_length=12,
        expected_serial_bytes=13,
        encoded_packet=b"encoded",
        raw_frame_hex="0102",
        encoded_hex="0304",
    )
    rendered = snapshot.render()
    assert "CMD_GET_VERSION (0x40)" in rendered
    assert "Payload Length: 5 bytes" in rendered
    assert f"CRC32: 0x{TEST_BROKEN_CRC:08X}" in rendered
