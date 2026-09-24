"""Property-based fuzzing tests for RPC protocol framing and COBS/R encoding. [SIL-2]"""

import pytest
from cobs import cobsr
from hypothesis import given, settings, strategies as st
from mcubridge.protocol.frame import DecodedFrame, parse_frame
from mcubridge.protocol.protocol import CRC_COVERED_HEADER_SIZE

EXPECTED_COBS_ERRORS = (cobsr.DecodeError, ValueError)


@pytest.mark.fuzz
@settings(max_examples=200, derandomize=True, deadline=None)
@given(raw_data=st.binary(max_size=256))
def test_frame_parsing_resilience_to_fuzzing(raw_data: bytes) -> None:
    """Fuzzing property: parse_frame must either decode safely or reject with ValueError."""
    try:
        decoded = parse_frame(raw_data)
        assert isinstance(decoded, DecodedFrame)
    except ValueError as exc:
        assert isinstance(exc, ValueError)


@pytest.mark.fuzz
@settings(max_examples=200, derandomize=True, deadline=None)
@given(raw_data=st.binary(max_size=256))
def test_cobs_decoding_resilience(raw_data: bytes) -> None:
    """Fuzzing property: COBS decoder must never crash with unhandled exceptions."""
    try:
        res = cobsr.decode(raw_data)
        assert isinstance(res, bytes)
    except EXPECTED_COBS_ERRORS as exc:
        assert isinstance(exc, EXPECTED_COBS_ERRORS)


@pytest.mark.fuzz
@settings(max_examples=200, derandomize=True, deadline=None)
@given(raw_data=st.binary(max_size=CRC_COVERED_HEADER_SIZE + 5))
def test_frame_header_parsing_resilience(raw_data: bytes) -> None:
    """Targeted property: header-sized byte slices must deterministically validate or fail."""
    try:
        decoded = parse_frame(raw_data)
        assert isinstance(decoded, DecodedFrame)
    except ValueError as exc:
        assert isinstance(exc, ValueError)


@pytest.mark.fuzz
@settings(max_examples=200, derandomize=True, deadline=None)
@given(data=st.binary(max_size=512))
def test_cobs_roundtrip_isomorphism(data: bytes) -> None:
    """Algebraic invariant: COBS/R encode then decode is an exact roundtrip isomorphism."""
    encoded = cobsr.encode(data)
    assert b"\x00" not in encoded
    assert cobsr.decode(encoded) == data
