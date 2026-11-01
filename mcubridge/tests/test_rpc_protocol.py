"""Canonical regression and property tests for Pigweed HDLC / RPC protocol helpers. [SIL-2]"""

from __future__ import annotations

from google.protobuf.message import Message as ProtobufMessage
from hypothesis import example, given
from hypothesis import strategies as st
from pw_hdlc.decode import FrameDecoder
from pw_rpc.internal.packet_pb2 import RpcPacket

from mcubridge.protocol import protocol
from tests.conftest import (
    st_analog_write,
    st_datastore_put,
    st_digital_write,
    st_file_write,
    st_mailbox_push,
    st_pin_mode,
    st_spi_transfer,
)


@given(
    command_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    sequence_id=st.integers(min_value=0, max_value=protocol.UINT16_MAX),
    payload=st.binary(min_size=0, max_size=protocol.MAX_PAYLOAD_SIZE),
)
@example(command_id=0, sequence_id=0, payload=b"")
@example(command_id=protocol.UINT16_MAX, sequence_id=protocol.UINT16_MAX, payload=b"X" * 256)
def test_hdlc_rpc_build_and_parse_roundtrip_property(command_id: int, sequence_id: int, payload: bytes) -> None:
    """Property: Any frame built with command ID, sequence, and payload roundtrips losslessly via HDLC."""
    raw = protocol.encode_hdlc_rpc(command_id, payload, call_id=sequence_id)
    assert raw.startswith(protocol.FRAME_DELIMITER)
    assert raw.endswith(protocol.FRAME_DELIMITER)

    decoder = FrameDecoder()
    frames = list(decoder.process(raw))
    assert len(frames) == 1
    frame = frames[0]
    assert frame.ok()
    assert frame.address == protocol.HDLC_ADDRESS_RPC

    pkt = RpcPacket()
    pkt.ParseFromString(frame.data)
    assert pkt.call_id == sequence_id
    assert pkt.payload == payload


@given(
    payload=st.binary(min_size=0, max_size=protocol.MAX_PAYLOAD_SIZE),
)
@example(payload=b"")
@example(payload=b"Hello HDLC console stream!\n")
def test_hdlc_console_build_and_parse_roundtrip_property(payload: bytes) -> None:
    """Property: Any raw console payload roundtrips losslessly via HDLC console address."""
    raw = protocol.encode_hdlc_console(payload)
    assert raw.startswith(protocol.FRAME_DELIMITER)
    assert raw.endswith(protocol.FRAME_DELIMITER)

    decoder = FrameDecoder()
    frames = list(decoder.process(raw))
    assert len(frames) == 1
    frame = frames[0]
    assert frame.ok()
    assert frame.address == protocol.HDLC_ADDRESS_CONSOLE
    assert frame.data == payload


@given(
    channel_id=st.integers(min_value=1, max_value=16),
    service_id=st.integers(min_value=1, max_value=0xFFFFFFFF),
    method_id=st.integers(min_value=1, max_value=0xFFFFFFFF),
    call_id=st.integers(min_value=1, max_value=protocol.UINT16_MAX),
    payload=st.binary(min_size=0, max_size=256),
)
def test_hdlc_direct_rpc_roundtrip_property(
    channel_id: int, service_id: int, method_id: int, call_id: int, payload: bytes
) -> None:
    """Property: Direct RPC convention (channel, service, method, call_id, payload) roundtrips losslessly."""
    raw = protocol.encode_hdlc_rpc(channel_id, service_id, method_id, call_id, payload)
    decoder = FrameDecoder()
    frames = list(decoder.process(raw))
    assert len(frames) == 1
    frame = frames[0]
    assert frame.ok()
    assert frame.address == protocol.HDLC_ADDRESS_RPC

    pkt = RpcPacket()
    pkt.ParseFromString(frame.data)
    assert pkt.channel_id == channel_id
    assert pkt.service_id == service_id
    assert pkt.method_id == method_id
    assert pkt.call_id == call_id
    assert pkt.payload == payload


@given(
    command_id=st.integers(0, 100),
    sequence_id=st.integers(0, 1000),
    payload=st.binary(min_size=4, max_size=128),
    corrupt_idx=st.integers(1, 4),
)
def test_hdlc_detects_corrupted_fcs(command_id: int, sequence_id: int, payload: bytes, corrupt_idx: int) -> None:
    """Property: Any bit flip in the FCS tail guarantees deterministic frame error detection."""
    raw = bytearray(protocol.encode_hdlc_rpc(command_id, payload, call_id=sequence_id))
    # Mutate a byte in the FCS before the final delimiter
    raw[-1 - corrupt_idx] ^= 0x01
    decoder = FrameDecoder()
    frames = list(decoder.process(bytes(raw)))
    assert len(frames) == 1
    assert not frames[0].ok()


@given(
    command_id=st.integers(min_value=0, max_value=100),
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
    """SIL-2 Property: Any Protobuf message payload embedded in an HDLC RpcPacket roundtrips losslessly."""
    raw = protocol.encode_hdlc_rpc(command_id, pb_msg, call_id=sequence_id)
    decoder = FrameDecoder()
    frames = list(decoder.process(raw))
    assert len(frames) == 1
    frame = frames[0]
    assert frame.ok()

    pkt = RpcPacket()
    pkt.ParseFromString(frame.data)
    assert pkt.call_id == sequence_id

    # Decode target protobuf type
    msg_type = type(pb_msg)
    decoded_msg = msg_type()
    decoded_msg.ParseFromString(pkt.payload)
    assert decoded_msg == pb_msg
