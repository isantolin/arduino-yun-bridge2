"""Consistent Overhead Byte Stuffing (COBS)

This implementation is adapted from a public domain version by
Craig McQueen.
"""

def encode(data: bytes) -> bytes:
    """Encode a byte string using COBS.
    Does NOT add the trailing zero byte delimiter.
    """
    if not isinstance(data, bytes):
        raise TypeError("Input must be bytes")

    final = bytearray()
    if not data:
        final.append(0x01)
        return bytes(final)

    code_ptr = 0
    final.append(0)  # Placeholder for the first code byte
    idx = 0

    for byte in data:
        if byte == 0:
            final[code_ptr] = len(final) - code_ptr
            code_ptr = len(final)
            final.append(0)  # Placeholder for the next code
        else:
            final.append(byte)
            if len(final) - code_ptr == 255:
                final[code_ptr] = 255
                if idx + 1 < len(data):
                    code_ptr = len(final)
                    final.append(0)  # Placeholder
        idx += 1

    final[code_ptr] = len(final) - code_ptr
    return bytes(final)


def decode(data: bytes) -> bytes:
    """Decode a COBS-encoded byte string.
    Assumes the trailing zero byte delimiter has already been removed.
    """
    if not isinstance(data, bytes):
        raise TypeError("Input must be bytes")

    final = bytearray()
    ptr = 0
    while ptr < len(data):
        code = data[ptr]
        if code == 0:
            raise ValueError("Invalid COBS: Zero byte found in encoded data")

        ptr += 1

        end = ptr + code - 1
        if end > len(data):
            raise ValueError("Invalid COBS: Not enough data for block")

        final.extend(data[ptr:end])
        ptr = end

        if code < 255 and ptr < len(data):
            final.append(0)

    return bytes(final)

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
