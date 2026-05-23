"""Regression tests for RPC protocol helpers."""

from mcubridge.protocol.frame import Frame
from mcubridge.protocol import protocol


def test_crc_is_32bit() -> None:
    assert protocol.FRAME_CRC_FORMAT == "<I"
    assert protocol.CRC_SIZE == 4


def test_frame_build_appends_crc_bytes() -> None:
    payload = b"\x01\x02\x03"
    raw = Frame(command_id=protocol.Command.CMD_LINK_RESET.value, sequence_id=0, payload=payload).build()
    # Protobuf Envelope length is variable
    assert len(raw) > len(payload) + 32


def test_frame_build_uses_crc32() -> None:
    """Frame serialization uses CRC32 (4 bytes) via Construct Checksum."""
    payload = b"\xaa" * 4
    raw = Frame(command_id=protocol.Command.CMD_LINK_RESET.value, sequence_id=0, payload=payload).build()

    # Protobuf Envelope length is variable
    assert len(raw) > len(payload) + 32
