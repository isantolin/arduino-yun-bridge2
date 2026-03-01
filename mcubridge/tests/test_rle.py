"""Tests for RLE compression implementation."""

from __future__ import annotations

import pytest
from mcubridge.protocol.rle import (
    ESCAPE_BYTE,
    compression_ratio,
    decode,
    encode,
    should_compress,
)


class TestRLEEncode:
    """Tests for RLE encoding."""

    def test_empty_input(self) -> None:
        """Empty input returns empty output."""
        assert encode(b"") == b""

    def test_single_byte(self) -> None:
        """Single byte passes through unchanged."""
        assert encode(b"A") == b"A"
        assert encode(b"\x00") == b"\x00"

    def test_no_runs(self) -> None:
        """Data with no runs passes through unchanged."""
        data = b"ABCDEF"
        assert encode(data) == data

    def test_short_run_not_encoded(self) -> None:
        """Runs shorter than MIN_RUN_LENGTH are not encoded."""
        # Run of 3 should NOT be encoded (break-even is 4)
        data = b"AAA"
        assert encode(data) == b"AAA"

    def test_min_run_encoded(self) -> None:
        """Runs of exactly MIN_RUN_LENGTH are encoded."""
        # Run of 4: ESCAPE, count-2=2, byte
        data = b"AAAA"
        expected = bytes([ESCAPE_BYTE, 2, ord("A")])
        assert encode(data) == expected

    def test_long_run(self) -> None:
        """Long runs are properly encoded."""
        # Run of 10: ESCAPE, count-2=8, byte
        data = b"A" * 10
        expected = bytes([ESCAPE_BYTE, 8, ord("A")])
        assert encode(data) == expected

    def test_escape_byte_handling(self) -> None:
        """Escape byte (0xFF) in input is properly escaped."""
        # Single 0xFF becomes: ESCAPE, 255 (special marker), 0xFF
        data = bytes([ESCAPE_BYTE])
        expected = bytes([ESCAPE_BYTE, 255, ESCAPE_BYTE])
        assert encode(data) == expected

    def test_mixed_data(self) -> None:
        """Mixed data with runs and literals."""
        # "ABBBBBCD" = A + run(5,B) + C + D
        data = b"ABBBBBCD"
        expected = b"A" + bytes([ESCAPE_BYTE, 3, ord("B")]) + b"CD"
        assert encode(data) == expected

    def test_multiple_runs(self) -> None:
        """Multiple runs in sequence."""
        # AAAABBBB = run(4,A) + run(4,B)
        data = b"AAAABBBB"
        expected = bytes([ESCAPE_BYTE, 2, ord("A"), ESCAPE_BYTE, 2, ord("B")])
        assert encode(data) == expected

    def test_null_bytes(self) -> None:
        """Null bytes are handled correctly."""
        data = b"\x00\x00\x00\x00"  # Run of 4 nulls
        expected = bytes([ESCAPE_BYTE, 2, 0])
        assert encode(data) == expected

    def test_max_run_length(self) -> None:
        """Very long runs are split at MAX_RUN_LENGTH."""
        # Run of 300 bytes should be split
        data = b"A" * 300
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data


class TestRLEDecode:
    """Tests for RLE decoding."""

    def test_empty_input(self) -> None:
        """Empty input returns empty output."""
        assert decode(b"") == b""

    def test_literal_only(self) -> None:
        """Literals pass through unchanged."""
        data = b"ABCDEF"
        assert decode(data) == data

    def test_encoded_run(self) -> None:
        """Encoded runs are properly expanded."""
        # ESCAPE, count-2=3, 'A' = AAAAA (5 A's)
        encoded = bytes([ESCAPE_BYTE, 3, ord("A")])
        assert decode(encoded) == b"AAAAA"

    def test_escaped_escape(self) -> None:
        """Escaped escape byte decodes to single 0xFF."""
        # ESCAPE, 255, 0xFF = single 0xFF (special marker)
        encoded = bytes([ESCAPE_BYTE, 255, ESCAPE_BYTE])
        assert decode(encoded) == bytes([ESCAPE_BYTE])

    def test_malformed_truncated(self) -> None:
        """Malformed data (truncated escape sequence) raises ValueError."""
        # ESCAPE without enough following bytes
        with pytest.raises(ValueError, match="Malformed RLE"):
            decode(bytes([ESCAPE_BYTE]))
        with pytest.raises(ValueError, match="Malformed RLE"):
            decode(bytes([ESCAPE_BYTE, 5]))


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
            b"\xff" * 100,
            b"A" * 257,  # Exactly at max run boundary
            b"A" * 258,  # Just over max run boundary
            b"A" * 1000,
            # Mixed content
            b"Start" + b"\x00" * 50 + b"Middle" + b"\xff" * 30 + b"End",
        ],
    )
    def test_roundtrip(self, data: bytes) -> None:
        """Encode then decode returns original data."""
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data

    def test_all_byte_values(self) -> None:
        """All possible byte values survive roundtrip."""
        data = bytes(range(256)) * 2
        assert decode(encode(data)) == data


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
        data = bytes([ESCAPE_BYTE if i % 2 == 0 else (i & 0xFE) for i in range(50)])
        assert should_compress(data) is False


class TestCompressionRatio:
    """Tests for compression ratio calculation."""

    def test_no_compression(self) -> None:
        """Incompressible data has ratio ~1.0."""
        data = b"ABCDEFGH"
        encoded = encode(data)
        ratio = compression_ratio(data, encoded)
        assert ratio == pytest.approx(1.0)

    def test_good_compression(self) -> None:
        """Highly compressible data has ratio > 1."""
        data = b"A" * 100
        encoded = encode(data)
        ratio = compression_ratio(data, encoded)
        assert ratio > 10  # 100 bytes -> 3 bytes = ratio of ~33

    def test_empty(self) -> None:
        """Empty input returns 0."""
        assert compression_ratio(b"", b"") == 0.0


class TestRealWorldScenarios:
    """Tests with realistic protocol payloads."""

    def test_console_output_with_spaces(self) -> None:
        """Console output with runs of spaces compresses well."""
        # Typical indented code output
        data = b"    if (x > 0) {\n        return true;\n    }\n"
        encoded = encode(data)
        assert len(encoded) < len(data)
        assert decode(encoded) == data

    def test_binary_sensor_data(self) -> None:
        """Binary sensor data with repeated values."""
        # Simulated ADC readings with some stuck values
        data = bytes([100, 100, 100, 100, 100, 102, 103, 101, 100, 100, 100, 100])
        encoded = encode(data)
        assert decode(encoded) == data

    def test_file_with_nulls(self) -> None:
        """Binary file with null padding."""
        # Common in firmware images
        data = b"Header" + b"\x00" * 50 + b"Data" + b"\x00" * 30
        encoded = encode(data)
        assert len(encoded) < len(data)
        assert decode(encoded) == data
