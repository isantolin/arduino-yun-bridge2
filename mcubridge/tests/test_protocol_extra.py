"""Extra coverage for mcubridge.protocol components."""

import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame


def test_frame_parse_payload_length_mismatch() -> None:
    # We need a frame where the header payload_len doesn't match the actual payload size.
    # This is hard to build with Frame.build, so we manually construct it.

    from mcubridge.protocol.frame import RPC_FRAME_HEADER, Int32ub
    from binascii import crc32

    version = protocol.PROTOCOL_VERSION
    actual_payload = b"ABC"
    claimed_len = 10  # Mismatch
    command_id = 0x40
    sequence_id = 0x01

    header_raw = RPC_FRAME_HEADER.build({
        "version": version,
        "payload_len": claimed_len,
        "command_id": command_id,
        "sequence_id": sequence_id,
    })

    # Combined data for CRC
    data_for_crc = header_raw + actual_payload
    crc = crc32(data_for_crc) & 0xFFFFFFFF

    raw_frame = data_for_crc + Int32ub.build(crc)

    # Construct should catch this because Bytes(this.header.payload_len) will fail
    # or the length check at line 126 will catch it if Construct somehow returns.
    with pytest.raises(ValueError) as exc:
        Frame.parse(raw_frame)
    assert "Incomplete frame" in str(exc.value) or "parsing failed" in str(exc.value)


def test_rle_encode_decode_edge_cases() -> None:
    from mcubridge.protocol import rle
    from mcubridge.protocol.structures import RLEPayload

    # Empty
    assert rle.encode(b"") == b""
    assert RLEPayload(b"").decode() == b""
    # No compression benefit
    assert rle.encode(b"ABC") == b"ABC"  # Wait, literals are as-is if not 0xFF

    # Literal 0xFF
    assert rle.encode(b"\xff") == b"\xff\xff\xff"

    # 2 and 3 0xFF (encoded as individual literal escapes since len < 4)
    assert rle.encode(b"\xff\xff") == b"\xff\xff\xff\xff\xff\xff"
    assert rle.encode(b"\xff\xff\xff") == b"\xff\xff\xff\xff\xff\xff\xff\xff\xff"

    # Large run
    data = b"A" * 300
    encoded = rle.encode(data)
    # 300 = 256 + 44
    # Run 1: 0xFF, 254 (256-2), 'A'
    # Run 2: 'A' repeated 44 times -> 0xFF, 42 (44-2), 'A'
    assert len(encoded) == 6
    assert RLEPayload(encoded).decode() == data

def test_topics_handshake_topic() -> None:
    from mcubridge.protocol.topics import (
        Topic,
        topic_path,
    )

    assert topic_path("prefix", Topic.SYSTEM, "handshake") == "prefix/system/handshake"
    assert topic_path("p", Topic.DIGITAL, "13", "read") == "p/d/13/read"
    assert topic_path("p", Topic.SPI, "transfer") == "p/spi/transfer"
    assert topic_path("p", Topic.DATASTORE, "key", "get") == "p/datastore/key/get"
    assert topic_path("p", Topic.FILE, "path/to/file", "read") == "p/file/path/to/file/read"
    assert topic_path("p", Topic.SHELL, "123", "kill") == "p/sh/123/kill"
    assert topic_path("p", Topic.CONSOLE, "write") == "p/console/write"
    assert topic_path("p", Topic.MAILBOX, "push") == "p/mailbox/push"

