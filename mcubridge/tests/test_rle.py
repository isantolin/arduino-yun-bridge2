"""Tests for RLE compression implementation."""

from __future__ import annotations

import pytest
from mcubridge.protocol import protocol
from mcubridge.protocol.rle import (
    RLE_TRANSFORM,
    should_compress,
)
from mcubridge.protocol.structures import RLEPayload


class TestRLEEncode:
    """Tests for RLE encoding."""

    def test_empty_input(self) -> None:
        """Empty input returns empty output."""
        assert RLE_TRANSFORM.build(b"") == b""

    def test_single_byte(self) -> None:
        """Single byte passes through unchanged."""
        assert RLE_TRANSFORM.build(b"A") == b"A"
        assert RLE_TRANSFORM.build(b"\x00") == b"\x00"

    def test_no_runs(self) -> None:
        """Data with no runs passes through unchanged."""
        data = b"ABCDEF"
        assert RLE_TRANSFORM.build(data) == data

    def test_short_run_not_encoded(self) -> None:
        """Runs shorter than MIN_RUN_LENGTH are not encoded."""
        # Run of 3 should NOT be encoded (break-even is 4)
        data = b"AAA"
        assert RLE_TRANSFORM.build(data) == b"AAA"

    def test_min_run_encoded(self) -> None:
        """Runs of exactly MIN_RUN_LENGTH are encoded."""
        # Run of 4: ESCAPE, count-2=2, byte
        data = b"AAAA"
        expected = bytes([protocol.RLE_ESCAPE_BYTE, 2, ord("A")])
        assert RLE_TRANSFORM.build(data) == expected

    def test_long_run(self) -> None:
        """Long runs are properly encoded."""
        # Run of 10: ESCAPE, count-2=8, byte
        data = b"A" * 10
        expected = bytes([protocol.RLE_ESCAPE_BYTE, 8, ord("A")])
        assert RLE_TRANSFORM.build(data) == expected

    def test_escape_byte_handling(self) -> None:
        """Escape byte (0xFD) in input is properly escaped."""
        # Single 0xFD becomes: ESCAPE, 255 (special marker), 0xFD
        data = bytes([protocol.RLE_ESCAPE_BYTE])
        expected = bytes([protocol.RLE_ESCAPE_BYTE, 255, protocol.RLE_ESCAPE_BYTE])
        assert RLE_TRANSFORM.build(data) == expected

    def test_mixed_data(self) -> None:
        """Mixed data with runs and literals."""
        # "ABBBBBCD" = A + run(5,B) + C + D
        data = b"ABBBBBCD"
        expected = b"A" + bytes([protocol.RLE_ESCAPE_BYTE, 3, ord("B")]) + b"CD"
        assert RLE_TRANSFORM.build(data) == expected

    def test_multiple_runs(self) -> None:
        """Multiple runs in sequence."""
        # AAAABBBB = run(4,A) + run(4,B)
        data = b"AAAABBBB"
        expected = bytes(
            [
                protocol.RLE_ESCAPE_BYTE,
                2,
                ord("A"),
                protocol.RLE_ESCAPE_BYTE,
                2,
                ord("B"),
            ]
        )
        assert RLE_TRANSFORM.build(data) == expected

    def test_null_bytes(self) -> None:
        """Null bytes are handled correctly."""
        data = b"\x00\x00\x00\x00"  # Run of 4 nulls
        expected = bytes([protocol.RLE_ESCAPE_BYTE, 2, 0])
        assert RLE_TRANSFORM.build(data) == expected


class TestRLEDecode:
    """Tests for RLE decoding."""

    def test_empty_input(self) -> None:
        """Empty input returns empty output."""
        assert RLEPayload(b"").decode() == b""

    def test_literal_only(self) -> None:
        """Literals pass through unchanged."""
        data = b"ABCDEF"
        assert RLEPayload(data).decode() == data

    def test_encoded_run(self) -> None:
        """Encoded runs are properly expanded."""
        # ESCAPE, count-2=3, 'A' = AAAAA (5 A's)
        encoded = bytes([protocol.RLE_ESCAPE_BYTE, 3, ord("A")])
        assert RLEPayload(encoded).decode() == b"AAAAA"

    def test_escaped_escape(self) -> None:
        """Escaped escape byte decodes to single 0xFD."""
        # ESCAPE, 255, 0xFD = single 0xFD (special marker)
        encoded = bytes([protocol.RLE_ESCAPE_BYTE, 255, protocol.RLE_ESCAPE_BYTE])
        assert RLEPayload(encoded).decode() == bytes([protocol.RLE_ESCAPE_BYTE])

    def test_malformed_truncated(self) -> None:
        """Malformed data (truncated escape sequence) raises ValueError."""
        # ESCAPE without enough following bytes
        with pytest.raises(ValueError, match="RLE decompression failed"):
            RLEPayload(bytes([protocol.RLE_ESCAPE_BYTE])).decode()
        with pytest.raises(ValueError, match="RLE decompression failed"):
            RLEPayload(bytes([protocol.RLE_ESCAPE_BYTE, 5])).decode()


class TestRLERoundtrip:
    """Roundtrip tests: encode then decode should return original."""

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"A",
            b"AB",
            b"ABC",
            b"AAAA",
            b"AAAABBBBCCCC",
            b"Hello, World!",
            bytes(range(256)),
            b"\x00" * 100,
            b"\xfd" * 100,
            b"A" * 257,  # Exactly at max run boundary
            b"A" * 258,  # Just over max run boundary
            b"A" * 1000,
            # Mixed content
            b"Start" + b"\x00" * 50 + b"Middle" + b"\xfd" * 30 + b"End",
        ],
    )
    def test_roundtrip(self, data: bytes) -> None:
        """Encode then decode returns original data."""
        encoded = RLE_TRANSFORM.build(data)
        decoded = RLEPayload(encoded).decode()
        assert decoded == data

    def test_all_byte_values(self) -> None:
        """All possible byte values survive roundtrip."""
        data = bytes(range(256)) * 2
        assert RLEPayload(RLE_TRANSFORM.build(data)).decode() == data


class TestShouldCompress:
    """Tests for compression heuristic."""

    def test_small_data(self) -> None:
        """Small data should not be compressed."""
        assert should_compress(b"AAAA") is False  # Too small
        assert should_compress(b"A" * 7) is False  # Still too small

    def test_no_runs(self) -> None:
        """Data without runs should not be compressed."""
        data = bytes(range(100))  # All different bytes
        assert should_compress(data) is False

    def test_good_runs(self) -> None:
        """Data with good runs should be compressed."""
        data = b"A" * 50  # Very compressible
        assert should_compress(data) is True

    def test_many_escapes(self) -> None:
        """Data with many escape bytes and no runs should not compress."""
        data = bytes(
            [protocol.RLE_ESCAPE_BYTE if i % 2 == 0 else (i & 0xFE) for i in range(50)]
        )
        assert should_compress(data) is False


class TestRealWorldScenarios:
    """Tests with realistic protocol payloads."""

    def test_console_output_with_spaces(self) -> None:
        """Console output with runs of spaces compresses well."""
        # Typical indented code output
        data = b"    if (x > 0) {\n        return true;\n    }\n"
        encoded = RLE_TRANSFORM.build(data)
        assert len(encoded) < len(data)
        assert RLEPayload(encoded).decode() == data

    def test_binary_sensor_data(self) -> None:
        """Binary sensor data with repeated values."""
        # Simulated ADC readings with some stuck values
        data = bytes([100, 100, 100, 100, 100, 102, 103, 101, 100, 100, 100, 100])
        encoded = RLE_TRANSFORM.build(data)
        assert RLEPayload(encoded).decode() == data

    def test_file_with_nulls(self) -> None:
        """Binary file with null padding."""
        # Common in firmware images
        data = b"Header" + b"\x00" * 50 + b"Data" + b"\x00" * 30
        encoded = RLE_TRANSFORM.build(data)
        assert len(encoded) < len(data)
        assert RLEPayload(encoded).decode() == data
