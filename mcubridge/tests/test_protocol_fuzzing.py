"""Property-based fuzzing tests for Pigweed HDLC framing and RPC packet parsing. [SIL-2]"""

from __future__ import annotations

import pytest
from google.protobuf.message import DecodeError as ProtobufDecodeError
from hypothesis import event, given, note, settings, target
from hypothesis import strategies as st
from pw_hdlc.decode import FrameDecoder
from pw_rpc.internal.packet_pb2 import RpcPacket

from mcubridge.protocol import protocol


@pytest.mark.fuzz
@settings(max_examples=200, derandomize=True, deadline=None)
@given(raw_data=st.binary(max_size=256))
def test_hdlc_decoder_resilience_to_fuzzing(raw_data: bytes) -> None:
    """Fuzzing property: FrameDecoder must process arbitrary binary data without unhandled exceptions."""
    decoder = FrameDecoder()
    frames = list(decoder.process(raw_data))
    assert isinstance(frames, list)
    for frame in frames:
        assert isinstance(frame.ok(), bool)


@pytest.mark.fuzz
@settings(max_examples=200, derandomize=True, deadline=None)
@given(raw_data=st.binary(max_size=256))
def test_rpc_packet_parsing_resilience(raw_data: bytes) -> None:
    """Fuzzing property: RpcPacket protobuf parsing must either succeed or raise ProtobufDecodeError."""
    pkt = RpcPacket()
    success = False
    try:
        pkt.ParseFromString(raw_data)
        success = pkt.IsInitialized()
    except ProtobufDecodeError as exc:
        success = bool(str(exc))
    assert success is True


@pytest.mark.fuzz
@settings(max_examples=60, derandomize=True, deadline=None)
@given(
    rpc_packets=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=0xFFFFFFFF),
            st.integers(min_value=1, max_value=0xFFFFFFFF),
            st.integers(min_value=0, max_value=1000),
            st.binary(min_size=0, max_size=128),
        ),
        min_size=1,
        max_size=10,
    ),
    chunk_sizes=st.lists(st.integers(min_value=1, max_value=16), min_size=1, max_size=50),
)
def test_hdlc_streaming_chunking_fragmentation_invariant(
    rpc_packets: list[tuple[int, int, int, bytes]],
    chunk_sizes: list[int],
) -> None:
    """SIL-2 Streaming Invariant: HDLC frame extraction across arbitrary byte fragmentation."""
    # 1. Build contiguous wire byte stream with HDLC UI framing
    expected_frames: list[tuple[int, int, int, bytes]] = []
    wire_parts: list[bytes] = []
    for service_id, method_id, call_id, payload in rpc_packets:
        frame_bytes = protocol.encode_hdlc_rpc(1, service_id, method_id, call_id, payload)
        wire_parts.append(frame_bytes)
        expected_frames.append((service_id, method_id, call_id, payload))
    wire_stream = b"".join(wire_parts)

    note(f"Total wire bytes: {len(wire_stream)}, expected frames: {len(expected_frames)}")
    target(float(len(chunk_sizes)), label="fragmentation_degree")

    # 2. Slice wire stream into arbitrary chunks according to chunk_sizes
    chunks: list[bytes] = []
    idx = 0
    size_iter = iter(chunk_sizes)
    while idx < len(wire_stream):
        size = next(size_iter, 7)
        end = idx + size
        chunks.append(wire_stream[idx:end])
        idx = end

    event(f"stream_chunks_{min(len(chunks), 20)}")

    # 3. Feed chunks into HDLC FrameDecoder incrementally
    decoder = FrameDecoder()
    extracted_frames: list[tuple[int, int, int, bytes]] = []
    for chunk in chunks:
        for frame in decoder.process(chunk):
            if frame.ok() and frame.address == protocol.HDLC_ADDRESS_RPC:
                pkt = RpcPacket()
                pkt.ParseFromString(frame.data)
                extracted_frames.append(
                    (
                        pkt.service_id,
                        pkt.method_id,
                        pkt.call_id,
                        pkt.payload,
                    )
                )

    # 4. Deterministic post-condition assertions
    assert len(extracted_frames) == len(expected_frames)
    for expected, actual in zip(expected_frames, extracted_frames, strict=True):
        assert actual[0] == expected[0]
        assert actual[1] == expected[1]
        assert actual[2] == expected[2]
        assert actual[3] == expected[3]
