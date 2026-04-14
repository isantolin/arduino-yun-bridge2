"""Extra coverage for mcubridge.protocol components."""

from __future__ import annotations

import pytest
from mcubridge.protocol.frame import Frame, parse_frame


def test_frame_repr_and_iter() -> None:
    frame = Frame(command_id=0x01, sequence_id=42, payload=b"data")

    # Test iteration/unpacking
    cmd, seq, pl = frame
    assert cmd == 0x01
    assert seq == 42
    assert pl == b"data"

    # Test properties
    assert frame.raw_command_id == 0x01
    assert frame.is_compressed is False


def test_parse_frame_incomplete() -> None:
    with pytest.raises(ValueError, match="Incomplete or malformed"):
        parse_frame(b"\x02\x00")
