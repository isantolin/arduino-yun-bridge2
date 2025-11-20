import pytest

from yunbridge.rpc.frame import Frame
from yunbridge.rpc import protocol


def test_build_and_parse_round_trip() -> None:
    payload = b"\x01\x02\x03"
    raw = Frame.build(0x1234, payload)

    assert (
        len(raw)
        == protocol.CRC_COVERED_HEADER_SIZE
        + len(payload)
        + protocol.CRC_SIZE
    )

    parsed_command, parsed_payload = Frame.parse(raw)
    assert parsed_command == 0x1234
    assert parsed_payload == payload


def test_build_rejects_large_payload() -> None:
    payload = b"a" * (protocol.MAX_PAYLOAD_SIZE + 1)

    with pytest.raises(ValueError):
        Frame.build(0x10, payload)


def test_build_rejects_invalid_command_id() -> None:
    with pytest.raises(ValueError):
        Frame.build(0x1_0000, b"")


def test_parse_rejects_short_frame() -> None:
    raw = b"short"

    with pytest.raises(ValueError):
        Frame.parse(raw)


def test_parse_detects_crc_mismatch() -> None:
    payload = b"valid"
    raw = Frame.build(0x20, payload)
    corrupted = raw[:-1] + bytes([raw[-1] ^ 0xFF])

    with pytest.raises(ValueError):
        Frame.parse(corrupted)


def test_parse_validates_version_and_length() -> None:
    payload = b"data"
    raw = bytearray(Frame.build(0x30, payload))

    raw[0] ^= 0x01
    with pytest.raises(ValueError):
        Frame.parse(bytes(raw))

    raw = bytearray(Frame.build(0x31, payload))
    raw[1] = 0x00
    raw[2] = 0x00
    with pytest.raises(ValueError):
        Frame.parse(bytes(raw))
