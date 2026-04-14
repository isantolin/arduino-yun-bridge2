"""Tests for the RPC frame building and parsing."""

from __future__ import annotations

import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.frame import Frame, build_frame, parse_frame

TEST_CMD_ID = protocol.Command.CMD_GET_VERSION


def test_frame_object_round_trip() -> None:
    frame = Frame(command_id=TEST_CMD_ID, sequence_id=0, payload=b"hello")
    raw = build_frame(frame)
    new_frame = parse_frame(raw)
    assert new_frame.command_id == frame.command_id
    assert new_frame.payload == frame.payload


def test_build_rejects_large_payload() -> None:
    payload = b"A" * (protocol.MAX_PAYLOAD_SIZE + 1)
    with pytest.raises(ValueError, match="Payload too large"):
        build_frame(
            Frame(
                command_id=protocol.Command.CMD_SET_PIN_MODE,
                sequence_id=0,
                payload=payload,
            )
        )


def test_parse_validates_version_and_length() -> None:
    # Manual raw frame with invalid version
    raw = bytearray(build_frame(Frame(command_id=TEST_CMD_ID, sequence_id=0, payload=b"")))
    raw[0] = 0xFF  # Invalid version
    with pytest.raises(ValueError):
        parse_frame(bytes(raw))

    # Invalid length
    with pytest.raises(ValueError):
        parse_frame(b"\x01")


def test_parse_detects_crc_mismatch() -> None:
    raw = bytearray(
        build_frame(Frame(command_id=TEST_CMD_ID, sequence_id=0, payload=b"hello"))
    )
    # Corrupt last byte of CRC
    raw[-1] ^= 0xFF
    with pytest.raises(ValueError, match="integrity check failure|CRC32|malformed"):
        parse_frame(bytes(raw))
