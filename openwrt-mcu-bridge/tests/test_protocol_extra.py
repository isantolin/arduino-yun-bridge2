"""Extra coverage for mcubridge.protocol components."""

import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame


def test_frame_parse_payload_length_mismatch() -> None:
    # We need a frame where the header payload_len doesn't match the actual payload size.
    # This is hard to build with Frame.build, so we manually construct it.

    import binascii
    import struct

    version = protocol.PROTOCOL_VERSION
    actual_payload = b"ABC"
    claimed_len = 10  # Mismatch
    command_id = 0x40

    # Header: version(1), claimed_len(2), command_id(2)
    header = struct.pack(">BHH", version, claimed_len, command_id)
    content = header + actual_payload

    crc = binascii.crc32(content)
    raw_frame = content + struct.pack(">I", crc)

    # Construct should catch this because Bytes(this.header.payload_len) will fail
    # or the length check at line 126 will catch it if Construct somehow returns.
    with pytest.raises(ValueError) as exc:
        Frame.parse(raw_frame)
    assert "length mismatch" in str(exc.value) or "parsing failed" in str(exc.value)


def test_rle_encode_decode_edge_cases() -> None:
    from mcubridge.protocol import rle

    # Empty
    assert rle.encode(b"") == b""
    assert rle.decode(b"") == b""

    # No compression benefit
    assert rle.encode(b"ABC") == b"ABC"  # Wait, literals are as-is if not 0xFF

    # Literal 0xFF
    assert rle.encode(b"\xff") == b"\xff\xff\xff"

    # 2 and 3 0xFF
    assert rle.encode(b"\xff\xff") == b"\xff\x00\xff"
    assert rle.encode(b"\xff\xff\xff") == b"\xff\x01\xff"

    # Large run
    data = b"A" * 300
    encoded = rle.encode(data)
    # 300 = 256 + 44
    # Run 1: 0xFF, 254 (256-2), 'A'
    # Run 2: 'A' repeated 44 times -> 0xFF, 42 (44-2), 'A'
    assert len(encoded) == 6
    assert rle.decode(encoded) == data


def test_topics_handshake_topic() -> None:
    from mcubridge.protocol.topics import (
        Topic,
        topic_path,
    )

    assert topic_path("prefix", Topic.SYSTEM, "handshake") == "prefix/system/handshake"
    assert topic_path("p", Topic.DIGITAL, "13", "read") == "p/d/13/read"
    assert topic_path("p", Topic.ANALOG, "0", "read") == "p/a/0/read"
    assert topic_path("p", Topic.DATASTORE, "get", "k") == "p/datastore/get/k"
    assert topic_path("p", Topic.FILE, "read", "f") == "p/file/read/f"
    assert topic_path("p", Topic.SHELL, "run") == "p/sh/run"
    assert topic_path("p", Topic.SHELL, "poll", "1") == "p/sh/poll/1"
    assert topic_path("p", Topic.MAILBOX, "incoming_available") == "p/mailbox/incoming_available"
    assert topic_path("p", Topic.MAILBOX, "outgoing_available") == "p/mailbox/outgoing_available"
