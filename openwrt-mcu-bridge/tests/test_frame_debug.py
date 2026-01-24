"""Tests for the frame_debug utility."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Fix import paths for tools and sibling tests
_REPO_ROOT = Path(__file__).parents[2]
_PKG_ROOT = Path(__file__).parents[1]

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from mcubridge.rpc.protocol import (
    Command,
    FRAME_DELIMITER,
    Status,
    UINT8_MASK,
)
from tools import frame_debug
from tests.test_constants import TEST_BROKEN_CRC


def test_resolve_command_hex() -> None:
    assert frame_debug._resolve_command(
        f"0x{Command.CMD_LINK_RESET.value:02X}"
    ) == Command.CMD_LINK_RESET.value
    assert frame_debug._resolve_command(f"0X{UINT8_MASK:02X}") == UINT8_MASK
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
    with pytest.raises(ValueError, match="even number of digits"):
        frame_debug._parse_payload("123")

    with pytest.raises(ValueError, match="Invalid payload hex"):
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
    assert f"cmd_id=0x{Command.CMD_GET_VERSION.value:02X} (CMD_GET_VERSION)" in rendered
    assert f"crc=0x{TEST_BROKEN_CRC:08X}" in rendered
    assert "raw_frame=0102" in rendered


def test_hex_with_spacing() -> None:
    assert frame_debug._hex_with_spacing(bytes([1, 2])) == "01 02"
    assert frame_debug._hex_with_spacing(b"") == ""


def test_build_snapshot() -> None:
    snapshot = frame_debug.build_snapshot(Command.CMD_GET_VERSION.value, b"")
    assert snapshot.command_id == Command.CMD_GET_VERSION.value
    assert snapshot.payload_length == 0
    assert snapshot.cobs_length > 0
    assert snapshot.encoded_packet.endswith(FRAME_DELIMITER)


def test_iter_counts() -> None:
    assert list(frame_debug._iter_counts(3)) == [0, 1, 2]

    # Test infinite generator (partial)
    gen = iter(frame_debug._iter_counts(0))
    assert next(gen) == 0
    assert next(gen) == 1
    assert next(gen) == 2


def test_main_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # Test running without --port (dry run)
    mock_open = MagicMock()
    monkeypatch.setattr(frame_debug, "_open_serial_device", mock_open)
    
    ret = frame_debug.main(
        [
            "--command",
            "CMD_GET_VERSION",
            "--count",
            "1",
        ]
    )
    assert ret == 0
    mock_open.assert_not_called()


def test_main_with_serial_write(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_open = MagicMock(return_value=123)
    mock_write = MagicMock(return_value=10)
    mock_close = MagicMock()
    
    monkeypatch.setattr(frame_debug, "_open_serial_device", mock_open)
    monkeypatch.setattr(frame_debug, "_write_frame", mock_write)
    monkeypatch.setattr(frame_debug.os, "close", mock_close)

    ret = frame_debug.main(
        [
            "--port",
            "/dev/ttyTEST",
            "--command",
            "CMD_GET_VERSION",
            "--count",
            "1",
        ]
    )

    assert ret == 0
    mock_open.assert_called_once()
    mock_write.assert_called()
    mock_close.assert_called_with(123)


def test_main_with_serial_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_open = MagicMock(return_value=123)
    mock_write = MagicMock(return_value=10)
    mock_read = MagicMock(return_value=None)
    mock_close = MagicMock()

    monkeypatch.setattr(frame_debug, "_open_serial_device", mock_open)
    monkeypatch.setattr(frame_debug, "_write_frame", mock_write)
    monkeypatch.setattr(frame_debug, "_read_frame", mock_read)
    monkeypatch.setattr(frame_debug.os, "close", mock_close)

    ret = frame_debug.main(
        [
            "--port",
            "/dev/ttyTEST",
            "--command",
            "CMD_GET_VERSION",
            "--count",
            "1",
            "--read-response",
        ]
    )

    assert ret == 0
    mock_read.assert_called()


def test_main_with_serial_read_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from cobs import cobs
    from mcubridge.rpc.frame import Frame as RpcFrame
    
    frame = RpcFrame(Command.CMD_GET_VERSION_RESP.value, b"v2.0")
    response = cobs.encode(frame.to_bytes()) + FRAME_DELIMITER

    mock_open = MagicMock(return_value=123)
    mock_write = MagicMock(return_value=10)
    mock_read = MagicMock(return_value=response)
    mock_close = MagicMock()

    monkeypatch.setattr(frame_debug, "_open_serial_device", mock_open)
    monkeypatch.setattr(frame_debug, "_write_frame", mock_write)
    monkeypatch.setattr(frame_debug, "_read_frame", mock_read)
    monkeypatch.setattr(frame_debug.os, "close", mock_close)

    with patch("tools.frame_debug._decode_frame") as mock_decode:
        mock_decode.return_value = frame_debug.Frame(
            Status.OK.value, b"response"
        )

        ret = frame_debug.main(
            [
                "--port",
                "/dev/ttyTEST",
                "--command",
                "CMD_GET_VERSION",
                "--count",
                "1",
                "--read-response",
            ]
        )

        assert ret == 0
        mock_decode.assert_called()


def test_main_invalid_args() -> None:
    # Invalid command
    # We need to patch sys.stderr to avoid printing to console during test
    with patch("sys.stderr"):
        # Let's test the ValueError path in main()
        # argparse will exit with 2
        with pytest.raises(SystemExit) as excinfo:
            frame_debug.main(["--command", ""])
        assert excinfo.value.code == 2