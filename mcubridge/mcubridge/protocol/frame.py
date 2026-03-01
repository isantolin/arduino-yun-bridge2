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

import msgspec
from mcubridge.protocol.structures import FRAME_STRUCT

from . import protocol


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

    @property
    def is_compressed(self) -> bool:
        """Return True if the frame command ID indicates RLE compression."""
        return bool(self.command_id & protocol.CMD_FLAG_COMPRESSED)

    @property
    def raw_command_id(self) -> int:
        """Return the command ID without the compression flag."""
        return self.command_id & ~protocol.CMD_FLAG_COMPRESSED

    @staticmethod
    def build(command_id: int, payload: bytes = b"") -> bytes:
        """Build a raw frame (header + payload + CRC) for COBS encoding.

        Implementation using 'construct' library for declarative serialization.
        """
        payload_len = len(payload)
        if payload_len > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload too large ({payload_len} bytes); " f"max is {protocol.MAX_PAYLOAD_SIZE}")
        if not 0 <= command_id <= protocol.UINT16_MAX:
            raise ValueError(f"Command id {command_id} outside 16-bit range")

        # [SIL-2] Decompose command ID for BitStruct is handled by CommandIdAdapter in structures.py
        return FRAME_STRUCT.build(
            {
                "content": {
                    "value": {
                        "header": {
                            "version": protocol.PROTOCOL_VERSION,
                            "payload_len": payload_len,
                            "command_id": command_id,
                        },
                        "payload": payload,
                    }
                }
            }
        )

    @staticmethod
    def parse(raw_frame_buffer: bytes | bytearray | memoryview) -> tuple[int, bytes]:
        """Parse a decoded frame and validate header, payload, and CRC.

        Implementation using 'construct' library for declarative parsing.
        """
        data_bytes = bytes(raw_frame_buffer)
        if len(data_bytes) < protocol.MIN_FRAME_SIZE:
            raise ValueError(f"Incomplete frame: size {len(data_bytes)} < {protocol.MIN_FRAME_SIZE}")

        try:
            container = FRAME_STRUCT.parse(data_bytes)
        except Exception as e:
            raise ValueError(f"Frame parsing failed: {e}") from e

        header = container.content.value.header
        if header.version != protocol.PROTOCOL_VERSION:
            raise ValueError(f"Invalid version {header.version} != {protocol.PROTOCOL_VERSION}")

        payload_bytes = container.content.value.payload
        if len(payload_bytes) != header.payload_len:
            raise ValueError(f"Payload length mismatch: {len(payload_bytes)} != {header.payload_len}")

        return int(header.command_id), payload_bytes

    def to_bytes(self) -> bytes:
        """Serialize the instance using :meth:`build`."""
        return self.build(self.command_id, self.payload)

    @classmethod
    def from_bytes(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame`."""
        command_id, payload = cls.parse(raw_frame_buffer)
        return cls(command_id=command_id, payload=payload)
