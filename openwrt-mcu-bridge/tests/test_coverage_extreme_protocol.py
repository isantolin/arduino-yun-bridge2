import struct
import pytest
from mcubridge.rpc.frame import Frame
from mcubridge.rpc import protocol

def test_frame_from_bytes_truncated_header():
    # Header is CRC_COVERED_HEADER_SIZE bytes. Provide less.
    with pytest.raises(ValueError, match="Incomplete frame"):
        Frame.from_bytes(b"\x01")

def test_frame_from_bytes_invalid_crc():
    # Build a valid-looking frame but change one byte to break CRC
    data = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 0, 0x40)
    data += b"payload"
    # Append a fake CRC
    data += b"\x00\x00\x00\x00"
    with pytest.raises(ValueError, match="CRC mismatch"):
        Frame.from_bytes(data)

def test_frame_from_bytes_payload_length_mismatch():
    # Header says 10 bytes payload, but provide 5.
    header = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 10, 0x40)
    data = header + b"12345" + b"\x00\x00\x00\x00"
    # Current implementation raises CRC mismatch because length check is implicit in CRC check
    with pytest.raises(ValueError, match="CRC mismatch"):
        Frame.from_bytes(data)

def test_frame_to_bytes_max_payload():
    max_payload = b"A" * protocol.MAX_PAYLOAD_SIZE
    frame = Frame(0x40, max_payload)
    data = frame.to_bytes()
    assert len(data) == protocol.CRC_COVERED_HEADER_SIZE + protocol.MAX_PAYLOAD_SIZE + protocol.CRC_SIZE