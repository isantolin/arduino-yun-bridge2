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
from construct import ConstructError
from mcubridge.protocol.structures import FRAME_STRUCT

from . import protocol


def _crc32_hash(data: bytes | bytearray | memoryview) -> int:
    """Compute CRC32 (IEEE 802.3) masked to 32-bit unsigned."""
    return crc32(data) & protocol.CRC32_MASK


class Frame(msgspec.Struct, frozen=True, kw_only=True):
    """Represents an RPC frame for MCU-Linux communication.

    This class provides both object-oriented and static methods for
    frame construction and parsing.

    Attributes:
        command_id: The RPC command or status code (16-bit).
        payload: The frame payload (0 to MAX_PAYLOAD_SIZE bytes).
    """

    command_id: int
    payload: bytes = b""

    @staticmethod
    def build(command_id: int, payload: bytes = b"") -> bytes:
        """Build a raw frame (header + payload + CRC) for COBS encoding.

        Delegates construction and CRC calculation to hardened FRAME_STRUCT.
        """
        try:
            return FRAME_STRUCT.build({
                "header": {
                    "version": protocol.PROTOCOL_VERSION,
                    "payload_len": len(payload),
                    "command_id": command_id,
                },
                "payload": payload,
                "crc": 0, # Checksum field will calculate this
            })
        except ConstructError as e:
            raise ValueError(f"Frame construction failed: {e}") from e

    @staticmethod
    def parse(raw_frame_buffer: bytes | bytearray | memoryview) -> tuple[int, bytes]:
        """Parse a decoded frame and validate header, payload, and CRC.

        Validation is performed declaratively by FRAME_STRUCT.
        """
        try:
            container = FRAME_STRUCT.parse(bytes(raw_frame_buffer))
            # command_id is now automatically an IntEnum (Command or Status)
            return int(container.header.command_id), container.payload
        except ConstructError as e:
            # Checksum failure or validation failure (Check) will land here
            raise ValueError(f"Frame validation failed: {e}") from e

    def to_bytes(self) -> bytes:
        """Serialize the instance using :meth:`build`."""
        return self.build(self.command_id, self.payload)

    @classmethod
    def from_bytes(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame`."""
        command_id, payload = cls.parse(raw_frame_buffer)
        return cls(command_id=command_id, payload=payload)
