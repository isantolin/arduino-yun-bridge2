"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
The frame format is designed for reliable communication:
- CRC32 integrity check on every frame (calculated via standard binascii)
- Explicit length fields prevent buffer overruns
- Version field ensures protocol compatibility
- Big-endian byte order for cross-platform consistency

Frame Structure (on wire after COBS encoding):
    [Header (5 bytes)] [Payload (0-64 bytes)] [CRC32 (4 bytes)]

Header Format (big-endian):
    - version (1 byte): Protocol version, must match PROTOCOL_VERSION
    - payload_length (2 bytes): Number of payload bytes
    - command_id (2 bytes): Command or status code from protocol.py

The raw frame is then COBS-encoded and terminated with 0x00 delimiter
before transmission.
"""

from __future__ import annotations

from binascii import crc32
import msgspec

from . import protocol


def _crc32_hash(data: bytes | bytearray | memoryview) -> int:
    """Compute CRC32 (IEEE 802.3) masked to 32-bit unsigned."""
    return crc32(data) & protocol.CRC32_MASK


class Frame(msgspec.Struct, frozen=True, kw_only=True):
    """Represents an RPC frame for MCU-Linux communication.

    This class provides both object-oriented and static methods for
    frame construction and parsing. Optimized for Zero-Copy parsing where possible.

    Attributes:
        command_id: The RPC command or status code (16-bit).
        payload: The frame payload (0 to MAX_PAYLOAD_SIZE bytes).
    """

    command_id: int
    payload: bytes = b""

    @staticmethod
    def build(command_id: int, payload: bytes = b"") -> bytes:
        """Build a raw frame (header + payload + CRC) for COBS encoding.

        Optimized native implementation replacing 'construct'.
        """
        payload_len = len(payload)
        if payload_len > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload too large ({payload_len} bytes); " f"max is {protocol.MAX_PAYLOAD_SIZE}")
        if not 0 <= command_id <= protocol.UINT16_MAX:
            raise ValueError(f"Command id {command_id} outside 16-bit range")

        # Header: Version (1) + Len (2) + Cmd (2) = 5 bytes
        # Native byte construction is significantly faster than struct/construct
        header = (
            protocol.PROTOCOL_VERSION.to_bytes(1, "big")
            + payload_len.to_bytes(2, "big")
            + command_id.to_bytes(2, "big")
        )

        body = header + payload
        crc = _crc32_hash(body)

        return body + crc.to_bytes(4, "big")

    @staticmethod
    def parse(raw_frame_buffer: bytes | bytearray | memoryview) -> tuple[int, bytes]:
        """Parse a decoded frame and validate header, payload, and CRC.

        Optimized native implementation using memoryview for zero-copy slicing.
        """
        # Ensure memoryview for efficient slicing
        mv = memoryview(raw_frame_buffer) if not isinstance(raw_frame_buffer, memoryview) else raw_frame_buffer
        total_len = len(mv)

        # 1. Verify minimum size (Header 5 + CRC 4 = 9 bytes)
        if total_len < protocol.MIN_FRAME_SIZE:
            raise ValueError(
                "Incomplete frame: size " f"{total_len} is less than minimum " f"{protocol.MIN_FRAME_SIZE}"
            )

        # 2. Verify header completeness
        # (covered by MIN_FRAME_SIZE check, but explicit check for logic clarity)
        header_size = protocol.CRC_COVERED_HEADER_SIZE
        if total_len < header_size + protocol.CRC_SIZE:
            raise ValueError("Incomplete header")

        # 3. Validate CRC
        # The body is everything EXCEPT the last 4 bytes (CRC)
        body = mv[:-4]
        received_crc_bytes = mv[-4:]
        received_crc = int.from_bytes(received_crc_bytes, "big")
        calculated_crc = _crc32_hash(body)

        if received_crc != calculated_crc:
            raise ValueError(f"CRC mismatch: expected 0x{calculated_crc:08X}, got 0x{received_crc:08X}")

        # 4. Parse Header
        # Version (offset 0, 1 byte)
        version = body[0]
        if version != protocol.PROTOCOL_VERSION:
            raise ValueError("Invalid version. Expected " f"{protocol.PROTOCOL_VERSION}, got {version}")

        # Payload Length (offset 1, 2 bytes)
        payload_len = int.from_bytes(body[1:3], "big")

        # Command ID (offset 3, 2 bytes)
        command_id = int.from_bytes(body[3:5], "big")

        # 5. Validate Payload Length against buffer size
        # We expected: Header (5) + Payload (N) = Body Length
        if len(body) != header_size + payload_len:
            raise ValueError(
                f"Frame size mismatch: header says {payload_len} payload bytes, "
                f"but buffer has {len(body) - header_size}"
            )

        if payload_len > protocol.MAX_PAYLOAD_SIZE:
             raise ValueError(f"Payload length {payload_len} exceeds max {protocol.MAX_PAYLOAD_SIZE}")

        # [SIL-2] Semantic Validation: Reject invalid/reserved command IDs
        if command_id < protocol.STATUS_CODE_MIN:
            raise ValueError(f"Invalid command id {command_id} (reserved/below minimum " f"{protocol.STATUS_CODE_MIN})")

        # Return command_id and payload (as bytes, creating a copy here is intended for storage)
        # Slicing body[5:] gives the payload.
        return command_id, body[5:].tobytes()

    def to_bytes(self) -> bytes:
        """Serialize the instance using :meth:`build`."""
        return self.build(self.command_id, self.payload)

    @classmethod
    def from_bytes(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame`."""
        command_id, payload = cls.parse(raw_frame_buffer)
        return cls(command_id=command_id, payload=payload)
