import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame
from mcubridge.protocol import mcubridge_pb2 as pb


def _wrap_payload(command_id: int, payload: bytes) -> bytes:
    rpc_payload = pb.RpcPayload()
    field_name = rpc_payload.DESCRIPTOR.fields_by_number[command_id].name
    target_msg = getattr(rpc_payload, field_name)
    if isinstance(target_msg, pb.ConsoleWrite):
        target_msg.data = payload
    elif not isinstance(target_msg, pb.Empty):
        try:
            target_msg.ParseFromString(payload)
        except Exception:
            pass  # Ignore for testing non-pb payloads if needed
    return rpc_payload.SerializeToString()


def test_build_and_parse_round_trip() -> None:
    payload = b"\x01\x02\x03"
    wrapped = _wrap_payload(protocol.Command.CMD_CONSOLE_WRITE, payload)
    raw = Frame(sequence_id=0, payload=wrapped).build()

    # Protobuf Envelope length is variable, but at least includes our data + overhead
    assert len(raw) > len(payload) + 32

    parsed_frame = Frame.parse(raw)
    assert parsed_frame.sequence_id == 0
    assert parsed_frame.payload == wrapped


def test_empty_payload_round_trip() -> None:
    wrapped = _wrap_payload(protocol.Command.CMD_GET_VERSION, b"")
    raw = Frame(sequence_id=0, payload=wrapped).build()
    parsed_frame = Frame.parse(raw)
    assert parsed_frame.sequence_id == 0
    assert parsed_frame.payload == wrapped


def test_max_payload_round_trip() -> None:
    payload = b"p" * (protocol.MAX_PAYLOAD_SIZE - 10)  # Account for protobuf overhead
    wrapped = _wrap_payload(protocol.Command.CMD_CONSOLE_WRITE, payload)
    raw = Frame(sequence_id=0, payload=wrapped).build()
    parsed_frame = Frame.parse(raw)
    assert parsed_frame.sequence_id == 0
    assert parsed_frame.payload == wrapped


def test_frame_object_round_trip() -> None:
    wrapped = _wrap_payload(protocol.Command.CMD_CONSOLE_WRITE, b"hello")
    frame = Frame(sequence_id=0, payload=wrapped)
    raw = frame.build()
    new_frame = Frame.parse(raw)
    assert new_frame.sequence_id == frame.sequence_id
    assert new_frame.payload == frame.payload


def test_build_rejects_large_payload() -> None:
    payload = b"a" * (protocol.MAX_PAYLOAD_SIZE + 1)

    with pytest.raises(ValueError):
        Frame(sequence_id=0, payload=payload).build()


def test_parse_rejects_short_frame() -> None:
    raw = b"short"

    with pytest.raises(ValueError):
        Frame.parse(raw)


def test_parse_detects_crc_mismatch() -> None:
    payload = b"valid"
    raw = Frame(sequence_id=0, payload=payload).build()
    corrupted = raw[:-1] + bytes([raw[-1] ^ protocol.UINT8_MASK])

    with pytest.raises(ValueError):
        Frame.parse(corrupted)


def test_parse_validates_version_and_length() -> None:
    payload = b"data"
    raw = bytearray(
        Frame(
            sequence_id=0,
            payload=payload,
        ).build()
    )

    raw[0] ^= 1
    with pytest.raises(ValueError):
        Frame.parse(bytes(raw))

    # Version is at the beginning of Protobuf, so corrupting early bytes might invalidate it
    raw = bytearray(
        Frame(
            sequence_id=0,
            payload=payload,
        ).build()
    )
    # We can't easily predict where version is without parsing, but let's try to mess it up
    # RpcEnvelope version is field 1, which usually starts with 0x08 (tag 1, wire type 0)
    if raw[0] == 0x08:
        raw[1] ^= 0xFF  # Corrupt version value

    with pytest.raises(ValueError):
        # It might fail either at CRC or version check
        Frame.parse(bytes(raw))
