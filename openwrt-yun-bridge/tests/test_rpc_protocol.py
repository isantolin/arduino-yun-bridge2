"""Regression tests for RPC protocol helpers."""
from yunbridge.rpc import frame, protocol


def test_crc_is_32bit() -> None:
    assert protocol.CRC_FORMAT == ">I"
    assert protocol.CRC_SIZE == 4


def test_frame_build_appends_crc_bytes() -> None:
    payload = b"\x01\x02\x03"
    raw = frame.Frame(
        protocol.Command.CMD_LINK_RESET.value,
        payload,
    ).to_bytes()
    expected_len = (
        protocol.CRC_COVERED_HEADER_SIZE
        + len(payload)
        + protocol.CRC_SIZE
    )
    assert len(raw) == expected_len
