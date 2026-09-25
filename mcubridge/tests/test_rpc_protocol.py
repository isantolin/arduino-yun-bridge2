"""Regression and property tests for RPC protocol frame helpers."""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from mcubridge.protocol.frame import build_frame, parse_frame
from mcubridge.protocol import protocol


def test_crc_is_32bit() -> None:
    assert protocol.CRC_SIZE == 4


@settings(max_examples=50, derandomize=True, deadline=None)
@given(
    command_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    sequence_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    payload=st.binary(min_size=0, max_size=protocol.MAX_PAYLOAD_SIZE),
)
def test_frame_build_and_parse_roundtrip_property(command_id: int, sequence_id: int, payload: bytes) -> None:
    """Property: Any frame built with valid command, sequence, and payload roundtrips losslessly."""
    raw = build_frame(command_id=command_id, sequence_id=sequence_id, payload=payload)
    assert len(raw) >= protocol.CRC_SIZE

    decoded = parse_frame(raw)
    assert decoded.envelope.command_id == command_id
    assert decoded.envelope.sequence_id == sequence_id
    assert decoded.envelope.version == protocol.PROTOCOL_VERSION
    assert decoded.payload == payload


@settings(max_examples=30, derandomize=True, deadline=None)
@given(
    command_id=st.integers(min_value=0x20, max_value=protocol.UINT16_MAX),
    sequence_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    payload=st.binary(min_size=1, max_size=1024),
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
