import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame, parse_frame
from tests.test_constants import TEST_CMD_ID


def test_build_and_parse_round_trip() -> None:
    payload = b"\x01\x02\x03"
    raw = build_frame(command_id=TEST_CMD_ID, sequence_id=0, payload=payload)

    # Protobuf Envelope length is variable, but at least includes our data + overhead
    assert len(raw) > len(payload) + 32

    envelope = parse_frame(raw)
    assert envelope.command_id == TEST_CMD_ID
    assert envelope.sequence_id == 0
    assert envelope.raw_payload == payload


def test_empty_payload_round_trip() -> None:
    raw = build_frame(command_id=TEST_CMD_ID, sequence_id=0, payload=b"")
    envelope = parse_frame(raw)
    assert envelope.command_id == TEST_CMD_ID
    assert envelope.sequence_id == 0
    assert envelope.raw_payload == b""


def test_max_payload_round_trip() -> None:
    payload = b"p" * protocol.MAX_PAYLOAD_SIZE
    raw = build_frame(command_id=TEST_CMD_ID, sequence_id=0, payload=payload)
    envelope = parse_frame(raw)
    assert envelope.command_id == TEST_CMD_ID
    assert envelope.sequence_id == 0
    assert envelope.raw_payload == payload


def test_build_rejects_large_payload() -> None:
    payload = b"a" * (protocol.MAX_PAYLOAD_SIZE + 1)

    with pytest.raises(ValueError):
        build_frame(command_id=protocol.Command.CMD_SET_PIN_MODE.value, sequence_id=0, payload=payload)


def test_build_rejects_invalid_command_id() -> None:
    with pytest.raises(ValueError):
        build_frame(command_id=protocol.UINT16_MAX + 1, sequence_id=0, payload=b"")


def test_parse_rejects_short_frame() -> None:
    raw = b"short"

    with pytest.raises(ValueError):
        parse_frame(raw)


def test_parse_detects_crc_mismatch() -> None:
    payload = b"valid"
    raw = build_frame(command_id=protocol.Command.CMD_CONSOLE_WRITE.value, sequence_id=0, payload=payload)
    corrupted = raw[:-1] + bytes([raw[-1] ^ protocol.UINT8_MASK])

    with pytest.raises(ValueError):
        parse_frame(corrupted)


def test_parse_validates_version_and_length() -> None:
    payload = b"data"
    raw = bytearray(
        build_frame(
            command_id=protocol.Command.CMD_DATASTORE_PUT.value,
            sequence_id=0,
            payload=payload,
        )
    )

    # Note: Protobuf encoded fields are not at fixed offsets.
    # This manual byte corruption is brittle for Protobuf messages but okay for basic error path testing.
    raw[0] ^= 1
    with pytest.raises(ValueError):
        parse_frame(bytes(raw))
