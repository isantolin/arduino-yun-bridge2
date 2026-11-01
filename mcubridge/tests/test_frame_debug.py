"""Tests for frame_debug tool."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from mcubridge.protocol import protocol
from mcubridge.protocol.protocol import UINT8_MASK, Command, Status
from pytest_mock import MockerFixture

from tools.emulation import frame_debug

TEST_BROKEN_CRC = 0xDEADBEEF

_VALID_CMD_NAMES = frozenset([entry.name.upper() for enum_cls in (Command, Status) for entry in enum_cls])


@given(val=st.integers(min_value=0, max_value=protocol.UINT16_MAX))
@example(val=0)
@example(val=protocol.UINT16_MAX)
def test_resolve_command_numeric_property(val: int) -> None:
    assert frame_debug.resolve_command(f"0x{val:X}") == val
    assert frame_debug.resolve_command(f"0x{val:x}") == val
    assert frame_debug.resolve_command(str(val)) == val


def test_resolve_command_name() -> None:
    assert frame_debug.resolve_command("CMD_GET_VERSION") == Command.CMD_GET_VERSION.value
    assert frame_debug.resolve_command("CMD_GET_FREE_MEMORY") == Command.CMD_GET_FREE_MEMORY.value
    # Case insensitive
    assert frame_debug.resolve_command("cmd_get_version") == Command.CMD_GET_VERSION.value


def test_resolve_command_empty() -> None:
    with pytest.raises(ValueError, match="command may not be empty"):
        frame_debug.resolve_command("")


@given(
    invalid_name=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=30).filter(
        lambda s: (
            s.strip().upper() not in _VALID_CMD_NAMES
            and not s.strip().startswith(("0x", "0X"))
            and not s.strip().isdigit()
            and bool(s.strip())
        )
    )
)
@example(invalid_name="INVALID_CMD")
def test_resolve_command_invalid_property(invalid_name: str) -> None:
    with pytest.raises(ValueError, match="Unknown command"):
        frame_debug.resolve_command(invalid_name)


@given(data=st.binary(min_size=0, max_size=512))
@example(data=b"")
def test_parse_payload_hex_property(data: bytes) -> None:
    assert frame_debug.parse_payload(data.hex()) == data
    assert frame_debug.parse_payload(data.hex(" ")) == data
    assert frame_debug.parse_payload(f"0x{data.hex()}") == data


def test_parse_payload_edge_cases() -> None:
    assert frame_debug.parse_payload(None) == b""


def test_parse_payload_invalid() -> None:
    # binascii.unhexlify raises binascii.Error: Odd-length string
    with pytest.raises(ValueError, match="Odd-length string"):
        frame_debug.parse_payload("123")

    with pytest.raises(ValueError, match="Invalid hex payload"):
        frame_debug.parse_payload("ZZ")


def test_name_for_command() -> None:
    assert frame_debug.name_for_command(Command.CMD_GET_VERSION.value) == "CMD_GET_VERSION"
    # Keep testing Status resolution
    assert frame_debug.name_for_command(Status.OK.value) == "OK"
    assert frame_debug.name_for_command(UINT8_MASK) == f"UNKNOWN(0x{UINT8_MASK:02X})"


def test_snapshot_render() -> None:
    snapshot = frame_debug.FrameDebugSnapshot(
        command_id=Command.CMD_GET_VERSION.value,
        command_name="CMD_GET_VERSION",
        payload_length=5,
        hdlc_length=12,
        encoded_packet=b"encoded_packet",
        encoded_hex="03 04",
    )
    rendered = snapshot.render()
    assert "CMD_GET_VERSION (0x40)" in rendered
    assert "Payload Length: 5 bytes" in rendered
    assert "HDLC Length: 12 bytes" in rendered
    assert "Encoded Frame (hex): 03 04" in rendered
    assert "Total Wire Bytes: 14 bytes" in rendered


def test_frame_debug_list_ports_empty(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    empty_list: list[Any] = []
    mocker.patch.object(frame_debug.serial.tools.list_ports, "comports", return_value=empty_list)
    frame_debug.list_ports()
    captured = capsys.readouterr()
    assert "No serial ports detected." in captured.out


def test_frame_debug_list_ports_found(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    from unittest.mock import MagicMock

    mock_port = MagicMock()
    mock_port.device = "/dev/ttyUSB0"
    mock_port.description = "CP2102 USB to UART"
    mock_port.vid = 0x10C4
    mock_port.pid = 0xEA60
    mock_port.serial_number = "0001"
    found_list: list[MagicMock] = [mock_port]
    mocker.patch.object(frame_debug.serial.tools.list_ports, "comports", return_value=found_list)
    frame_debug.list_ports()
    captured = capsys.readouterr()
    assert "Found 1 serial port(s):" in captured.out
    assert "/dev/ttyUSB0" in captured.out
    assert "CP2102 USB to UART" in captured.out
    assert "VID:PID=10C4:EA60" in captured.out
    assert "SER=0001" in captured.out


def test_frame_debug_generate_missing_command() -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(frame_debug.cli, ["generate"])
    assert result.exit_code != 0
    assert "Missing option" in result.output or "Usage:" in result.output
