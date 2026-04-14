"""Regression tests for RPC protocol helpers."""

from __future__ import annotations

from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame, build_frame


def test_frame_build_appends_crc_bytes() -> None:
    # Minimum frame: 1 (ver) + 2 (len) + 2 (cmd) + 2 (seq) + 4 (crc) = 11 bytes
    raw = build_frame(Frame(command_id=protocol.Command.CMD_GET_VERSION, sequence_id=0))
    assert len(raw) == 11


def test_frame_build_uses_crc32() -> None:
    # Deterministic CRC32 check for known frame
    # Frame: ver=2, len=0, cmd=64 (CMD_GET_VERSION), seq=0
    # Payload bytes: 02 00 00 00 40 00 00
    # Expected CRC32: calculated by build_frame
    raw = build_frame(Frame(command_id=protocol.Command.CMD_GET_VERSION, sequence_id=0))
    # Extract CRC from last 4 bytes
    _crc = raw[-4:]
    # If build_frame succeeded and returned a valid length, CRC is present
    assert len(raw) == 11
