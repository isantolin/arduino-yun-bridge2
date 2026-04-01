import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame
from tests.test_constants import TEST_CMD_ID


def test_build_and_parse_round_trip() -> None:
    payload = b"\x01\x02\x03"
    raw = Frame(command_id=TEST_CMD_ID, sequence_id=0, payload=payload).build()

    assert len(raw) == protocol.CRC_COVERED_HEADER_SIZE + len(payload) + protocol.CRC_SIZE

    parsed_command, parsed_seq, parsed_payload = Frame.parse(raw)
    assert parsed_command == TEST_CMD_ID
    assert parsed_seq == 0
    assert parsed_payload == payload


def test_empty_payload_round_trip() -> None:
    raw = Frame(command_id=TEST_CMD_ID, sequence_id=0, payload=b"").build()
    parsed_command, parsed_seq, parsed_payload = Frame.parse(raw)
    assert parsed_command == TEST_CMD_ID
    assert parsed_seq == 0
    assert parsed_payload == b""


def test_max_payload_round_trip() -> None:
    payload = b"p" * protocol.MAX_PAYLOAD_SIZE
    raw = Frame(command_id=TEST_CMD_ID, sequence_id=0, payload=payload).build()
    parsed_command, parsed_seq, parsed_payload = Frame.parse(raw)
    assert parsed_command == TEST_CMD_ID
    assert parsed_seq == 0
    assert parsed_payload == payload


def test_frame_object_round_trip() -> None:
    frame = Frame(command_id=TEST_CMD_ID, sequence_id=0, payload=b"hello")
    raw = frame.build()
    new_frame = Frame.parse(raw)
    assert new_frame.command_id == frame.command_id
    assert new_frame.payload == frame.payload


def test_build_rejects_large_payload() -> None:
    payload = b"a" * (protocol.MAX_PAYLOAD_SIZE + 1)

    with pytest.raises(ValueError):
        Frame(command_id=protocol.Command.CMD_SET_PIN_MODE, sequence_id=0, payload=payload).build()


def test_build_rejects_invalid_command_id() -> None:
    with pytest.raises(ValueError):
        Frame(command_id=protocol.UINT16_MAX + 1, sequence_id=0, payload=b"").build()


def test_parse_rejects_short_frame() -> None:
    raw = b"short"

    with pytest.raises(ValueError):
        Frame.parse(raw)


def test_parse_detects_crc_mismatch() -> None:
    payload = b"valid"
    raw = Frame(command_id=protocol.Command.CMD_CONSOLE_WRITE, sequence_id=0, payload=payload).build()
    corrupted = raw[:-1] + bytes([raw[-1] ^ protocol.UINT8_MASK])

    with pytest.raises(ValueError):
        Frame.parse(corrupted)


def test_parse_validates_version_and_length() -> None:
    payload = b"data"
    raw = bytearray(Frame(command_id=protocol.Command.CMD_DATASTORE_PUT, sequence_id=0, payload=payload).build())

    raw[0] ^= 1
    with pytest.raises(ValueError):
        Frame.parse(bytes(raw))

    raw = bytearray(Frame(command_id=protocol.Command.CMD_DATASTORE_GET, sequence_id=0, payload=payload).build())
    raw[1] = 0
    raw[2] = 0
    with pytest.raises(ValueError):
        Frame.parse(bytes(raw))
