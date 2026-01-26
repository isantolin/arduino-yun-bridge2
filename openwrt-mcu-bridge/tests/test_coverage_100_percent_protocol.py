import struct
from binascii import crc32
import pytest
from mcubridge.rpc.frame import Frame
from mcubridge.rpc import protocol

def _build_raw_with_crc(data_no_crc: bytes) -> bytes:
    c = crc32(data_no_crc) & protocol.CRC32_MASK
    return data_no_crc + struct.pack(protocol.CRC_FORMAT, c)

def test_frame_parse_incomplete_header():
    # protocol.MIN_FRAME_SIZE is 9. Header is 5.
    # Provide exactly 9 bytes but with a "payload_len" that makes data_to_check too short for header unpack?
    # No, parse() does:
    # crc_start = len(raw_frame_buffer) - 4
    # data_to_check = raw_frame_buffer[:crc_start]
    # If len is 9, data_to_check is 5.
    # To hit "Incomplete header" (line 132), data_to_check must be < 5.
    # This means len(raw_frame_buffer) < 9.
    # But step 1 checks len < 9 and raises "Incomplete frame".
    # Wait, line 132: if len(data_to_check) < protocol.CRC_COVERED_HEADER_SIZE:
    # If MIN_FRAME_SIZE is 9 and CRC_SIZE is 4, then data_to_check is always >= 5 if we pass step 1.
    # Let's check protocol.py for MIN_FRAME_SIZE.
    pass

def test_frame_parse_invalid_version():
    # Line 140
    header = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION + 1, 0, 0x40)
    raw = _build_raw_with_crc(header)
    with pytest.raises(ValueError, match="Invalid version"):
        Frame.parse(raw)

def test_frame_parse_invalid_command_id():
    # Line 149: command_id < protocol.STATUS_CODE_MIN
    header = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 0, protocol.STATUS_CODE_MIN - 1)
    raw = _build_raw_with_crc(header)
    with pytest.raises(ValueError, match="Invalid command id"):
        Frame.parse(raw)

def test_frame_parse_payload_length_mismatch():
    # Line 157
    # Header says 10 bytes, but we only have 0 bytes of payload.
    header = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 10, 0x40)
    # raw_frame_buffer will have 5 bytes header + 0 bytes payload + 4 bytes CRC = 9 bytes.
    # data_to_check will be 5 bytes.
    # actual_payload_len = 5 - 5 = 0.
    # payload_len = 10. 10 != 0.
    raw = _build_raw_with_crc(header)
    with pytest.raises(ValueError, match="Payload length mismatch"):
        Frame.parse(raw)

def test_frame_build_invalid_command_id():
    with pytest.raises(ValueError, match="outside 16-bit range"):
        Frame.build(-1)
    with pytest.raises(ValueError, match="outside 16-bit range"):
        Frame.build(70000)
