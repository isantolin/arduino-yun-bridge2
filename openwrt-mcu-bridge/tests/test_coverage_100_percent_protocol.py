from binascii import crc32

import pytest
from construct import ConstructError
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame


def _build_raw_with_crc(data_no_crc: bytes) -> bytes:
    c = crc32(data_no_crc) & protocol.CRC32_MASK
    return data_no_crc + protocol.CRC_STRUCT.build(c)


def test_frame_parse_coverage_all_errors():
    # Line 120: Incomplete header / Construct StreamError
    # To hit this, we need MIN_FRAME_SIZE to be smaller than Header + CRC
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protocol, "MIN_FRAME_SIZE", 5)
        # raw_frame_buffer must be >= 5 but < 9
        # Construct will fail to read payload/crc
        with pytest.raises(ValueError, match="(Incomplete header|Frame parsing failed)"):
            Frame.parse(b"1234567")

    # Line 125: CRC Mismatch
    bad_crc_frame = protocol.CRC_COVERED_HEADER_STRUCT.build(dict(
        version=protocol.PROTOCOL_VERSION,
        payload_len=0,
        command_id=0x40
    ))
    bad_crc_frame += b"\x00\x00\x00\x00"
    with pytest.raises(ValueError, match="CRC mismatch"):
        Frame.parse(bad_crc_frame)

    # Line 140: Invalid version
    bad_ver = protocol.CRC_COVERED_HEADER_STRUCT.build(dict(
        version=255,
        payload_len=0,
        command_id=0x40
    ))
    with pytest.raises(ValueError, match="Invalid version"):
        Frame.parse(_build_raw_with_crc(bad_ver))

    # Line 157: Payload length mismatch
    bad_len = protocol.CRC_COVERED_HEADER_STRUCT.build(dict(
        version=protocol.PROTOCOL_VERSION,
        payload_len=10,
        command_id=0x40
    ))
    raw = _build_raw_with_crc(bad_len)
    with pytest.raises(ValueError, match="(Frame size mismatch|Frame parsing failed)"):
        Frame.parse(raw)


def test_frame_wrappers_and_min_size():
    # Cover line 107: Incomplete frame (default check)
    with pytest.raises(ValueError, match="Incomplete frame"):
        Frame.parse(b"123")

    # Cover line 144: to_bytes wrapper
    f = Frame(command_id=0x40, payload=b"123")
    assert f.to_bytes() == Frame.build(0x40, b"123")

    # Cover line 151: from_bytes wrapper (implicitly used in other tests but ensuring explicit coverage)
    f2 = Frame.from_bytes(f.to_bytes())
    assert f2 == f


def test_frame_build_edge_cases():
    with pytest.raises(ValueError, match="outside 16-bit range"):
        Frame.build(-1)
    with pytest.raises(ValueError, match="outside 16-bit range"):
        Frame.build(70000)
    with pytest.raises(ValueError, match="Payload too large"):
        Frame.build(0x40, b"A" * 1000)
