import pytest

from mcubridge.rpc.frame import Frame
from mcubridge.rpc import protocol
from tests.test_constants import TEST_CMD_ID


def test_build_and_parse_round_trip() -> None:
    payload = b"\x01\x02\x03"
    raw = Frame.build(TEST_CMD_ID, payload)

    assert len(raw) == protocol.CRC_COVERED_HEADER_SIZE + len(payload) + protocol.CRC_SIZE

    parsed_command, parsed_payload = Frame.parse(raw)
    assert parsed_command == TEST_CMD_ID
    assert parsed_payload == payload


def test_build_rejects_large_payload() -> None:
    payload = b"a" * (protocol.MAX_PAYLOAD_SIZE + 1)

    with pytest.raises(ValueError):
        Frame.build(protocol.Command.CMD_SET_PIN_MODE, payload)


def test_build_rejects_invalid_command_id() -> None:
    with pytest.raises(ValueError):
        Frame.build(protocol.UINT16_MAX + 1, b"")


def test_parse_rejects_short_frame() -> None:
    raw = b"short"

    with pytest.raises(ValueError):
        Frame.parse(raw)


def test_parse_detects_crc_mismatch() -> None:
    payload = b"valid"
    raw = Frame.build(protocol.Command.CMD_CONSOLE_WRITE, payload)
    corrupted = raw[:-1] + bytes([raw[-1] ^ protocol.UINT8_MASK])

    with pytest.raises(ValueError):
        Frame.parse(corrupted)


def test_parse_validates_version_and_length() -> None:
    payload = b"data"
    raw = bytearray(Frame.build(protocol.Command.CMD_DATASTORE_PUT, payload))

    raw[0] ^= 1
    with pytest.raises(ValueError):
        Frame.parse(bytes(raw))

    raw = bytearray(Frame.build(protocol.Command.CMD_DATASTORE_GET, payload))
    raw[1] = 0
    raw[2] = 0
    with pytest.raises(ValueError):
        Frame.parse(bytes(raw))
