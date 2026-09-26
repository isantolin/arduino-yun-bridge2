"""Canonical regression and property tests for RPC protocol frame helpers. [SIL-2]"""

from __future__ import annotations

from binascii import crc32
from hypothesis import example, given, strategies as st
import pytest

from google.protobuf.message import Message as ProtobufMessage

from mcubridge.protocol.frame import build_frame, parse_frame
from mcubridge.protocol import is_system_command, mcubridge_pb2 as pb, protocol
from tests.test_constants import TEST_CMD_ID
from tests.conftest import (
    st_analog_write,
    st_datastore_put,
    st_digital_write,
    st_file_write,
    st_mailbox_push,
    st_pin_mode,
    st_spi_transfer,
)


def test_crc_is_32bit() -> None:
    assert protocol.CRC_SIZE == 4


@given(
    command_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    sequence_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    payload=st.binary(min_size=0, max_size=protocol.MAX_PAYLOAD_SIZE),
)
@example(command_id=0, sequence_id=0, payload=b"")
@example(command_id=protocol.UINT16_MAX, sequence_id=protocol.UINT16_MAX, payload=b"X" * protocol.MAX_PAYLOAD_SIZE)
def test_frame_build_and_parse_roundtrip_property(command_id: int, sequence_id: int, payload: bytes) -> None:
    """Property: Any frame built with valid command, sequence, and payload roundtrips losslessly."""
    raw = build_frame(command_id=command_id, sequence_id=sequence_id, payload=payload)
    assert len(raw) >= protocol.CRC_SIZE

    decoded = parse_frame(raw)
    assert decoded.envelope.command_id == command_id
    assert decoded.envelope.sequence_id == sequence_id
    assert decoded.envelope.version == protocol.PROTOCOL_VERSION
    assert decoded.payload == payload


@given(
    command_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX).filter(lambda cid: not is_system_command(cid)),
    sequence_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    payload=st.binary(min_size=1, max_size=protocol.MAX_PAYLOAD_SIZE),
    session_key=st.binary(min_size=32, max_size=32),
    nonce=st.binary(min_size=12, max_size=12),
)
def test_frame_build_and_parse_encrypted_roundtrip_property(
    command_id: int, sequence_id: int, payload: bytes, session_key: bytes, nonce: bytes
) -> None:
    """Property: Any authenticated & encrypted frame roundtrips losslessly under AEAD ChaCha20-Poly1305."""
    raw = build_frame(
        command_id=command_id,
        sequence_id=sequence_id,
        payload=payload,
        session_key=session_key,
        nonce=nonce,
    )
    decoded = parse_frame(raw, session_key=session_key)
    assert decoded.envelope.command_id == command_id
    assert decoded.envelope.sequence_id == sequence_id
    assert decoded.payload == payload


@given(
    command_id=st.integers(min_value=protocol.STATUS_CODE_MIN, max_value=protocol.SYSTEM_COMMAND_MAX),
    sequence_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    payload=st.binary(min_size=1, max_size=protocol.MAX_PAYLOAD_SIZE),
    session_key=st.binary(min_size=32, max_size=32),
    nonce=st.binary(min_size=12, max_size=12),
)
def test_frame_system_command_skips_encryption_property(
    command_id: int, sequence_id: int, payload: bytes, session_key: bytes, nonce: bytes
) -> None:
    """Property: System and status commands are exempt from AEAD encryption even when session_key is provided."""
    raw = build_frame(
        command_id=command_id,
        sequence_id=sequence_id,
        payload=payload,
        session_key=session_key,
        nonce=nonce,
    )
    decoded = parse_frame(raw, session_key=None)
    assert decoded.envelope.command_id == command_id
    assert decoded.envelope.sequence_id == sequence_id
    assert decoded.payload == payload


@given(
    command_id=st.integers(0, protocol.UINT16_MAX),
    sequence_id=st.integers(0, protocol.UINT16_MAX),
    payload=st.binary(max_size=protocol.MAX_PAYLOAD_SIZE),
    corrupt_idx=st.integers(0, 3),
)
def test_parse_detects_corrupted_crc(command_id: int, sequence_id: int, payload: bytes, corrupt_idx: int) -> None:
    """Property: Any bit flip in the 4-byte CRC tail guarantees deterministic ValueError detection."""
    raw = bytearray(build_frame(command_id=command_id, sequence_id=sequence_id, payload=payload))
    crc_pos = len(raw) - protocol.CRC_SIZE + corrupt_idx
    raw[crc_pos] ^= 0x01
    with pytest.raises(ValueError, match="CRC mismatch"):
        parse_frame(bytes(raw))


@given(
    command_id=st.integers(protocol.UINT16_MAX + 1, protocol.UINT16_MAX + 10000),
    sequence_id=st.integers(0, protocol.UINT16_MAX),
)
@example(command_id=-1, sequence_id=0)
def test_build_rejects_out_of_bounds_command_id(command_id: int, sequence_id: int) -> None:
    with pytest.raises(ValueError, match="Invalid command ID"):
        build_frame(command_id=command_id, sequence_id=sequence_id, payload=b"")


@given(
    command_id=st.integers(0, protocol.UINT16_MAX),
    sequence_id=st.integers(0, protocol.UINT16_MAX),
    payload=st.binary(min_size=protocol.MAX_PAYLOAD_SIZE + 1, max_size=protocol.MAX_PAYLOAD_SIZE + 128),
)
def test_build_rejects_oversized_payload(command_id: int, sequence_id: int, payload: bytes) -> None:
    with pytest.raises(ValueError, match="exceeds maximum"):
        build_frame(command_id=command_id, sequence_id=sequence_id, payload=payload)


def test_parse_rejects_short_frame() -> None:
    with pytest.raises(ValueError, match="Incomplete frame: too short"):
        parse_frame(b"sho")
    with pytest.raises(ValueError, match="Incomplete frame: too short"):
        parse_frame(b"")


def test_parse_validates_version_and_length() -> None:
    raw = bytearray(
        build_frame(
            command_id=protocol.Command.CMD_DATASTORE_PUT.value,
            sequence_id=0,
            payload=b"data",
        )
    )
    raw[0] ^= 1
    with pytest.raises(ValueError):
        parse_frame(bytes(raw))


def test_parse_frame_memoryview_and_bytearray() -> None:
    payload = b"zero-copy-test"
    raw = build_frame(command_id=TEST_CMD_ID, sequence_id=42, payload=payload)

    decoded_mv = parse_frame(memoryview(raw))
    assert decoded_mv.envelope.command_id == TEST_CMD_ID
    assert decoded_mv.envelope.sequence_id == 42
    assert decoded_mv.payload == payload

    decoded_ba = parse_frame(bytearray(raw))
    assert decoded_ba.envelope.command_id == TEST_CMD_ID
    assert decoded_ba.envelope.sequence_id == 42
    assert decoded_ba.payload == payload


def test_parse_frame_protobuf_decode_error() -> None:
    corrupt_body = b"\xff\xff\xff\xff"
    crc_bytes = (crc32(corrupt_body) & protocol.CRC32_MASK).to_bytes(protocol.CRC_SIZE, "little")
    raw = corrupt_body + crc_bytes
    with pytest.raises(ValueError, match="Failed to parse Protobuf envelope"):
        parse_frame(raw)


def test_parse_frame_invalid_version() -> None:
    env = pb.RpcEnvelope(version=999, command_id=1, sequence_id=1)
    body = env.SerializeToString()
    crc_bytes = (crc32(body) & protocol.CRC32_MASK).to_bytes(protocol.CRC_SIZE, "little")
    raw = body + crc_bytes
    with pytest.raises(ValueError, match="Unsupported protocol version"):
        parse_frame(raw)


def test_parse_frame_aead_decryption_failed() -> None:
    key = b"\x05" * protocol.AEAD_KEY_SIZE
    wrong_key = b"\x06" * protocol.AEAD_KEY_SIZE
    raw = build_frame(command_id=0x10, sequence_id=1, payload=b"secret", session_key=key)
    with pytest.raises(ValueError, match="AEAD decryption failed"):
        parse_frame(raw, session_key=wrong_key)


def test_parse_frame_empty_oneof_payload() -> None:
    env = pb.RpcEnvelope(version=protocol.PROTOCOL_VERSION, command_id=1, sequence_id=1)
    body = env.SerializeToString()
    crc_bytes = (crc32(body) & protocol.CRC32_MASK).to_bytes(protocol.CRC_SIZE, "little")
    raw = body + crc_bytes
    decoded = parse_frame(raw)
    assert decoded.payload == b""


@given(
    command_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    sequence_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    pb_msg=st.one_of(
        st_pin_mode(),
        st_digital_write(),
        st_analog_write(),
        st_datastore_put(),
        st_mailbox_push(),
        st_file_write(),
        st_spi_transfer(),
    ),
)
def test_protobuf_message_payload_roundtrip_property(
    command_id: int, sequence_id: int, pb_msg: ProtobufMessage
) -> None:
    """SIL-2 Property: Any Protobuf message payload embedded in an RpcEnvelope roundtrips losslessly."""
    raw = build_frame(command_id=command_id, sequence_id=sequence_id, payload=pb_msg)
    decoded = parse_frame(raw)
    assert decoded.envelope.command_id == command_id
    assert decoded.envelope.sequence_id == sequence_id
    assert decoded.envelope.version == protocol.PROTOCOL_VERSION
    assert decoded.payload == pb_msg


@given(
    command_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX).filter(lambda cid: not is_system_command(cid)),
    sequence_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    pb_msg=st.one_of(
        st_pin_mode(),
        st_digital_write(),
        st_analog_write(),
        st_datastore_put(),
        st_mailbox_push(),
        st_file_write(),
        st_spi_transfer(),
    ),
    session_key=st.binary(min_size=32, max_size=32),
    nonce=st.binary(min_size=12, max_size=12),
)
def test_protobuf_message_encrypted_roundtrip_property(
    command_id: int, sequence_id: int, pb_msg: ProtobufMessage, session_key: bytes, nonce: bytes
) -> None:
    """SIL-2 Property: Protobuf message encrypted under AEAD roundtrips losslessly to raw bytes."""
    raw = build_frame(
        command_id=command_id,
        sequence_id=sequence_id,
        payload=pb_msg,
        session_key=session_key,
        nonce=nonce,
    )
    decoded = parse_frame(raw, session_key=session_key)
    assert decoded.envelope.command_id == command_id
    assert decoded.envelope.sequence_id == sequence_id
    assert decoded.payload == pb_msg.SerializeToString()
