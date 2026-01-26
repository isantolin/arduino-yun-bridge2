import struct
from binascii import crc32
import pytest
from mcubridge.rpc.frame import Frame
from mcubridge.rpc import protocol

def _build_raw_with_crc(data_no_crc: bytes) -> bytes:
    c = crc32(data_no_crc) & protocol.CRC32_MASK
    return data_no_crc + struct.pack(protocol.CRC_FORMAT, c)

def test_frame_parse_coverage_all_errors():
    # Line 120: Incomplete header
    # To hit this, we need MIN_FRAME_SIZE to be smaller than Header + CRC
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protocol, "MIN_FRAME_SIZE", 5)
        # raw_frame_buffer must be >= 5 but < 9
        with pytest.raises(ValueError, match="Incomplete header"):
            Frame.parse(b"1234567")

    # Line 125: CRC Mismatch
    bad_crc_frame = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 0, 0x40) + b"\x00\x00\x00\x00"
    with pytest.raises(ValueError, match="CRC mismatch"):
        Frame.parse(bad_crc_frame)

    # Line 140: Invalid version
    bad_ver = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, 255, 0, 0x40)
    with pytest.raises(ValueError, match="Invalid version"):
        Frame.parse(_build_raw_with_crc(bad_ver))

    # Line 149: Invalid command id (reserved)
    bad_cmd = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 0, 0)
    with pytest.raises(ValueError, match="Invalid command id"):
        Frame.parse(_build_raw_with_crc(bad_cmd))

    # Line 157: Payload length mismatch
    bad_len = struct.pack(protocol.CRC_COVERED_HEADER_FORMAT, protocol.PROTOCOL_VERSION, 10, 0x40)
    raw = _build_raw_with_crc(bad_len)
    with pytest.raises(ValueError, match="Payload length mismatch"):
        Frame.parse(raw)

def test_frame_build_edge_cases():
    with pytest.raises(ValueError, match="outside 16-bit range"):
        Frame.build(-1)
    with pytest.raises(ValueError, match="outside 16-bit range"):
        Frame.build(70000)
    with pytest.raises(ValueError, match="Payload too large"):
        Frame.build(0x40, b"A" * 1000)
