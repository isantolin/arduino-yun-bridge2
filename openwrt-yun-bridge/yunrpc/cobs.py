"""Consistent Overhead Byte Stuffing (COBS)

This implementation is adapted from a public domain version by
Craig McQueen.
"""

from typing import Generator


def encode(data: bytes) -> bytes:
    """COBS encode data.

    Args:
        data: Data to encode.

    Returns:
        Encoded data.
    """
    encoded = bytearray()
    # Add a zero to the end of the data
    data = bytearray(data)
    data.append(0)
    #
    code_index = 0
    code = 1
    for byte in data:
        if byte == 0:
            encoded.append(code)
            code = 1
        else:
            encoded.append(byte)
            code += 1
            if code == 255:
                encoded.insert(code_index, code)
                code = 1
                code_index = len(encoded)
    encoded.insert(code_index, code)
    return bytes(encoded)


def decode(encoded: bytes) -> bytes:
    """COBS decode data.

    Args:
        encoded: COBS-encoded data (without the trailing zero delimiter).

    Returns:
        Decoded data.
    """
    decoded = bytearray()
    code = 255
    copy = False
    for byte in encoded:
        if copy:
            decoded.append(byte)
            copy -= 1
        else:
            copy = byte - 1
            code = byte
            if code != 255:
                decoded.append(0)
    # The COBS decoding process often prepends an implicit zero.
    # This slice removes that prepended zero to yield the original data.
    decoded = decoded[1:]
    return bytes(decoded)


def iter_decode(encoded: bytes) -> Generator[bytes, None, None]:
    """COBS decode data, and yield each packet.

    Args:
        encoded: Data to decode.

    Returns:
        A generator that yields decoded packets.
    """
    packet = bytearray()
    for byte in encoded:
        if byte == 0:
            yield bytes(packet)
            packet = bytearray()
        else:
            packet.append(byte)


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
