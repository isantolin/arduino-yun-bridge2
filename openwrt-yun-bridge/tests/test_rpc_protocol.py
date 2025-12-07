"""Regression tests for RPC protocol helpers."""
import pytest

from yunbridge.rpc import frame, protocol


def test_crc_is_32bit() -> None:
    assert protocol.CRC_FORMAT == ">I"
    assert protocol.CRC_SIZE == 4


def test_frame_build_appends_crc_bytes() -> None:
    payload = b"\x01\x02\x03"
    raw = frame.Frame(
        protocol.Command.CMD_LINK_RESET.value,
        payload,
    ).to_bytes()
    expected_len = (
        protocol.CRC_COVERED_HEADER_SIZE
        + len(payload)
        + protocol.CRC_SIZE
    )
    assert len(raw) == expected_len


def test_frame_build_masks_crc_to_protocol_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frame serialization must honor the CRC size from the protocol spec."""

    monkeypatch.setattr(frame.protocol, "CRC_FORMAT", ">H", raising=False)
    monkeypatch.setattr(frame.protocol, "CRC_SIZE", 2, raising=False)

    payload = b"\xAA" * 4
    raw = frame.Frame(
        protocol.Command.CMD_LINK_RESET.value,
        payload,
    ).to_bytes()

    expected_len = (
        protocol.CRC_COVERED_HEADER_SIZE
        + len(payload)
        + frame.protocol.CRC_SIZE
    )
    assert len(raw) == expected_len
