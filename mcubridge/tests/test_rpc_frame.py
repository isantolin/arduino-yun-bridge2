"""Tests for RPC frame building, parsing, and property-based isomorphism. [SIL-2]"""

import pytest
from hypothesis import given, settings, strategies as st
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import build_frame, parse_frame
from tests.test_constants import TEST_CMD_ID


@settings(max_examples=100, derandomize=True, deadline=None)
@given(
    command_id=st.integers(0, protocol.UINT16_MAX),
    sequence_id=st.integers(0, protocol.UINT16_MAX),
    payload=st.binary(max_size=protocol.MAX_PAYLOAD_SIZE),
)
def test_frame_roundtrip_isomorphism(command_id: int, sequence_id: int, payload: bytes) -> None:
    """Property: Any valid (cmd, seq, payload) roundtrips through build_frame and parse_frame without mutation."""
    raw = build_frame(command_id=command_id, sequence_id=sequence_id, payload=payload)
    assert len(raw) >= len(payload) + protocol.CRC_SIZE
    decoded = parse_frame(raw)
    assert decoded.envelope.command_id == command_id
    assert decoded.envelope.sequence_id == sequence_id
    assert decoded.payload == payload


@settings(max_examples=50, derandomize=True, deadline=None)
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
    with pytest.raises(ValueError):
        parse_frame(bytes(raw))


@settings(max_examples=50, derandomize=True, deadline=None)
@given(
    command_id=st.integers(protocol.UINT16_MAX + 1, protocol.UINT16_MAX + 10000),
    sequence_id=st.integers(0, protocol.UINT16_MAX),
)
def test_build_rejects_out_of_bounds_command_id(command_id: int, sequence_id: int) -> None:
    with pytest.raises(ValueError):
        build_frame(command_id=command_id, sequence_id=sequence_id, payload=b"")


@settings(max_examples=50, derandomize=True, deadline=None)
@given(
    command_id=st.integers(0, protocol.UINT16_MAX),
    sequence_id=st.integers(0, protocol.UINT16_MAX),
    payload=st.binary(min_size=protocol.MAX_PAYLOAD_SIZE + 1, max_size=protocol.MAX_PAYLOAD_SIZE + 128),
)
def test_build_rejects_oversized_payload(command_id: int, sequence_id: int, payload: bytes) -> None:
    with pytest.raises(ValueError):
        build_frame(command_id=command_id, sequence_id=sequence_id, payload=payload)


def test_parse_rejects_short_frame() -> None:
    with pytest.raises(ValueError):
        parse_frame(b"short")


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

    # Test with memoryview (zero-copy buffer protocol)
    decoded_mv = parse_frame(memoryview(raw))
    assert decoded_mv.envelope.command_id == TEST_CMD_ID
    assert decoded_mv.envelope.sequence_id == 42
    assert decoded_mv.payload == payload

    # Test with bytearray (mutable buffer protocol)
    decoded_ba = parse_frame(bytearray(raw))
    assert decoded_ba.envelope.command_id == TEST_CMD_ID
    assert decoded_ba.envelope.sequence_id == 42
    assert decoded_ba.payload == payload
