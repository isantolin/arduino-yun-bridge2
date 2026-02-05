"""Regression tests for RPC protocol helpers."""

from mcubridge.protocol import frame, protocol


def test_crc_is_32bit() -> None:
    assert protocol.CRC_FORMAT == ">I"
    assert protocol.CRC_SIZE == 4


def test_frame_build_appends_crc_bytes() -> None:
    payload = b"\x01\x02\x03"
    raw = frame.Frame.build(protocol.Command.CMD_LINK_RESET.value, payload)
    expected_len = protocol.CRC_COVERED_HEADER_SIZE + len(payload) + protocol.CRC_SIZE
    assert len(raw) == expected_len


def test_frame_build_uses_crc32() -> None:
    """Frame serialization uses CRC32 (4 bytes) via Construct Checksum."""
    payload = b"\xaa" * 4
    raw = frame.Frame.build(protocol.Command.CMD_LINK_RESET.value, payload)

    # CRC is always 4 bytes (Int32ub) via Construct Checksum
    expected_len = protocol.CRC_COVERED_HEADER_SIZE + len(payload) + 4
    assert len(raw) == expected_len
