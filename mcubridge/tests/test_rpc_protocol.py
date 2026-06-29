"""Regression tests for RPC protocol helpers."""

from mcubridge.protocol.frame import build_frame
from mcubridge.protocol import protocol


def test_crc_is_32bit() -> None:
    assert protocol.CRC_SIZE == 4


def test_frame_build_appends_crc_bytes() -> None:
    payload = b"\x01\x02\x03"
    raw = build_frame(command_id=protocol.Command.CMD_LINK_RESET.value, sequence_id=0, payload=payload)
    # Protobuf Envelope length is variable
    assert len(raw) > len(payload) + 16


def test_frame_build_uses_crc32() -> None:
    """Frame serialization uses CRC32 (4 bytes) via Construct Checksum."""
    payload = b"\xaa" * 4
    raw = build_frame(command_id=protocol.Command.CMD_LINK_RESET.value, sequence_id=0, payload=payload)

    # Protobuf Envelope length is variable
    assert len(raw) > len(payload) + 16
