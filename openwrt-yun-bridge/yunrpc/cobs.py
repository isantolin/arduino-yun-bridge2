"""Consistent Overhead Byte Stuffing (COBS) helpers.

This module keeps the Python implementation aligned with the firmware logic
so frame encoding/decoding behaves exactly the same on both sides of the
serial link.
"""

from __future__ import annotations

from typing import Generator

_MAX_CODE: int = 0xFF


def encode(data: bytes) -> bytes:
    """COBS-encode *data*.

    The returned buffer does **not** include the trailing ``0x00`` delimiter;
    callers must append it when framing the stream, matching the behaviour of
    the MCU implementation in ``cobs.h``.
    """

    if not data:
        return b"\x01"

    encoded = bytearray()
    code_index = 0
    encoded.append(0)  # placeholder for the code byte
    code = 1

    for byte in data:
        if byte == 0:
            encoded[code_index] = code
            code_index = len(encoded)
            encoded.append(0)  # new placeholder
            code = 1
            continue

        encoded.append(byte)
        code += 1
        if code == _MAX_CODE:
            encoded[code_index] = code
            code_index = len(encoded)
            encoded.append(0)
            code = 1

    encoded[code_index] = code
    return bytes(encoded)


def decode(encoded: bytes) -> bytes:
    """Decode a COBS frame that excludes the delimiter byte."""

    decoded = bytearray()
    index = 0
    length = len(encoded)

    while index < length:
        code = encoded[index]
        index += 1

        if code == 0:
            raise ValueError("COBS encoded data may not contain zero bytes")

        read_length = code - 1
        if index + read_length > length:
            raise ValueError("COBS encoded data is truncated")

        decoded.extend(encoded[index:index + read_length])
        index += read_length

        if code < _MAX_CODE and index < length:
            decoded.append(0)

    return bytes(decoded)


def iter_decode(encoded: bytes) -> Generator[bytes, None, None]:
    """Yield decoded packets from a stream with zero-delimited frames."""

    packet = bytearray()
    for byte in encoded:
        if byte == 0:
            if packet:
                yield decode(bytes(packet))
                packet.clear()
        else:
            packet.append(byte)

    if packet:
        yield decode(bytes(packet))


if __name__ == "__main__":
    test_cases = [
        (b"\x00", b"\x01\x01"),
        (b"\x00\x00", b"\x01\x01\x01"),
        (b"A\x00B", b"\x02A\x02B"),
        (b"ABC", b"\x04ABC"),
        (b"\x11\x22\x00\x33", b"\x03\x11\x22\x02\x33"),
        (bytes(range(1, 255)), b"\xff" + bytes(range(1, 255))),
        (bytes(range(1, 256)), b"\xff" + bytes(range(1, 255)) + b"\x02\xff"),
    ]

    for i, (decoded, encoded) in enumerate(test_cases):
        print(f"--- Test Case {i+1} ---")
        print(f"Original:  {decoded.hex()}")
        print(f"Expected:  {encoded.hex()}")

        # Test encoding
        calculated_encoded = encode(decoded)
        print(f"Encoded:   {calculated_encoded.hex()}")
        assert calculated_encoded == encoded, f"Encode failed for case {i+1}"
        print("Encode PASSED")

        # Test decoding
        calculated_decoded = decode(encoded)
        print(f"Decoded:   {calculated_decoded.hex()}")
        assert calculated_decoded == decoded, f"Decode failed for case {i+1}"
        print("Decode PASSED")

    print("\nAll COBS tests passed!")
