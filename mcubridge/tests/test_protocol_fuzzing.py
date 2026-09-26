import pytest
from cobs import cobsr
from hypothesis import event, given, note, settings, strategies as st, target
from mcubridge.protocol.frame import DecodedFrame, build_frame, parse_frame
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


@pytest.mark.fuzz
@settings(max_examples=60, derandomize=True, deadline=None)
@given(
    commands=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=255),
            st.integers(min_value=0, max_value=1000),
            st.binary(min_size=0, max_size=128),
        ),
        min_size=1,
        max_size=10,
    ),
    chunk_sizes=st.lists(st.integers(min_value=1, max_value=16), min_size=1, max_size=50),
)
def test_streaming_chunking_fragmentation_invariant(
    commands: list[tuple[int, int, bytes]],
    chunk_sizes: list[int],
) -> None:
    """SIL-2 Streaming Invariant: COBS frame extraction across arbitrary byte fragmentation."""
    # 1. Build contiguous wire byte stream with framing delimiter 0x00
    expected_frames: list[tuple[int, int, bytes]] = []
    wire_parts: list[bytes] = []
    for cmd_id, seq_id, payload in commands:
        frame_bytes = build_frame(cmd_id, seq_id, payload)
        wire_parts.append(cobsr.encode(frame_bytes) + b"\x00")
        expected_frames.append((cmd_id, seq_id, payload))
    wire_stream = b"".join(wire_parts)

    note(f"Total wire bytes: {len(wire_stream)}, expected frames: {len(expected_frames)}")
    target(float(len(chunk_sizes)), label="fragmentation_degree")

    # 2. Slice wire stream into arbitrary chunks according to chunk_sizes
    chunks: list[bytes] = []
    idx = 0
    size_iter = iter(chunk_sizes)
    while idx < len(wire_stream):
        size = next(size_iter, 7)
        chunks.append(wire_stream[idx : idx + size])
        idx += size

    event(f"stream_chunks_{min(len(chunks), 20)}")

    # 3. Feed chunks into incremental streaming buffer
    buffer = bytearray()
    extracted_frames: list[tuple[int, int, bytes]] = []
    for chunk in chunks:
        buffer.extend(chunk)
        while b"\x00" in buffer:
            packet, _, rest = buffer.partition(b"\x00")
            buffer = bytearray(rest)
            if packet:
                decoded_cobs = cobsr.decode(bytes(packet))
                decoded_frame = parse_frame(decoded_cobs)
                payload_val = (
                    decoded_frame.payload
                    if isinstance(decoded_frame.payload, bytes)
                    else decoded_frame.payload.SerializeToString()
                )
                extracted_frames.append(
                    (
                        decoded_frame.envelope.command_id,
                        decoded_frame.envelope.sequence_id,
                        payload_val,
                    )
                )

    # 4. Deterministic post-condition assertions
    assert len(buffer) == 0, f"Dangling incomplete bytes in stream buffer: {bytes(buffer)!r}"
    assert len(extracted_frames) == len(expected_frames)
    for expected, actual in zip(expected_frames, extracted_frames, strict=True):
        assert actual[0] == expected[0]
        assert actual[1] == expected[1]
        assert actual[2] == expected[2]
